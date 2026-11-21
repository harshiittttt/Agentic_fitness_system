// Component tests that run the REAL screens (WakeCoach, Live) against fakes of the phone: the speech engine and the backend.
// Run:  npm i --no-save jest@29.7.0 jest-expo@52.0.6 react-test-renderer@18.3.1 @testing-library/react-native@12.9.0 --legacy-peer-deps   then   npx jest
module.exports = {
  preset: 'jest-expo',
  testMatch: ['<rootDir>/__tests__/**/*.test.{ts,tsx}'],
  setupFiles: ['<rootDir>/__tests__/setup.js'],
  testTimeout: 20000,
};
