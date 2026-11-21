jest.mock('expo-speech-recognition', () => require('./fakeSpeech'));
jest.mock('@react-native-async-storage/async-storage', () => require('@react-native-async-storage/async-storage/jest/async-storage-mock'));
jest.mock('react-native-safe-area-context', () => require('react-native-safe-area-context/jest/mock').default);
jest.mock('expo-constants', () => ({ __esModule: true, default: { expoConfig: { version: 'test' } } }));
jest.mock('expo-haptics', () => ({ impactAsync: jest.fn(), notificationAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: {}, NotificationFeedbackType: {} }));
jest.mock('expo-speech', () => ({ speak: jest.fn((t, o) => o && o.onDone && setTimeout(o.onDone, 0)), stop: jest.fn(), getAvailableVoicesAsync: jest.fn(async () => []) }));
jest.mock('expo-av', () => ({ Audio: { setAudioModeAsync: jest.fn(), Sound: { createAsync: jest.fn() } } }));
jest.mock('expo-file-system', () => ({ cacheDirectory: '/tmp/', getInfoAsync: jest.fn(async () => ({ exists: false })), downloadAsync: jest.fn(), deleteAsync: jest.fn() }));
jest.mock('@expo/vector-icons', () => ({ Ionicons: 'Ionicons' }));
jest.mock('expo-linear-gradient', () => ({ LinearGradient: require('react-native').View }));
global.__dlog = [];
jest.mock('../src/diag', () => ({ dlog: (k, d) => { global.__dlog.push([k, d]); }, installCrashLog: () => {} }));
try { Object.defineProperty(require('react-native').AppState, 'currentState', { value: 'active', configurable: true, writable: true }); } catch (e) { /* ignore */ }
