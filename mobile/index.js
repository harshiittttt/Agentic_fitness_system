import React from 'react';
import { registerRootComponent } from 'expo';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import App from './App';

// SafeAreaProvider is the library's recommended root; it is required on web and harmless on native.
function Root() {
  return <SafeAreaProvider><App /></SafeAreaProvider>;
}

registerRootComponent(Root);
