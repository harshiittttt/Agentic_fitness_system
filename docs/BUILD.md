# Building the Android app (EAS)

Expo Go cannot load voice input (`expo-speech-recognition`) or push notifications, so you need a real build.
All commands run in `mobile/`. Steps 1 to 3 are interactive and need your Expo account.

## One-time setup

1. Make a free account at https://expo.dev.
2. Install and log in:
   ```
   cd mobile
   npm i -g eas-cli        # or prefix every command below with: npx eas-cli
   eas login
   ```
3. Link the project (writes `expo.extra.eas.projectId` and `owner` into `app.json`):
   ```
   eas init
   ```
   Answer yes to creating the project `ai-fitness-coach`. Commit the changed `app.json`.

## Build for your 4-5 people (standalone APK)

```
eas build --profile preview --platform android
```

- First run asks to generate an Android keystore: answer yes (EAS stores it).
- Takes roughly 10-20 minutes in the cloud queue. When done it prints a page URL and a QR code.
- The `preview` profile has no dev client, so the APK runs on its own with no laptop needed.

### Installing and sharing
- Open the build page link (https://expo.dev/accounts/<you>/projects/ai-fitness-coach/builds) on the phone, tap Install / download the APK, allow "install unknown apps" for the browser, then open it.
- To share: send the same build page link (or the QR) to the others. They open it on their Android phone and install. Anyone with the link can download it, so share it only with them.
- Each new build means a new link; people reinstall over the old app (keeps data as long as `android.package` is unchanged). Bump `android.versionCode` in `app.json` before every build you redistribute.

## Development build (optional, for editing code live)

```
eas build --profile development --platform android
npx expo start --dev-client
```
Install the APK, open it, and it connects to Metro like Expo Go but with voice and push working.

## Server address
No server URL is baked into the build. The app asks for the backend address when it first opens, so run the backend somewhere the phones can reach (same Wi-Fi LAN address, or a hosted URL).

## Push notifications
- The projectId from `eas init` is what the app uses to request an Expo push token (read from `Constants.expoConfig.extra.eas.projectId`). Push tokens only work in a built app, not Expo Go, and only after `eas init` has run.
- Android push delivery needs FCM credentials: run `eas credentials` (Android, then Push Notifications / FCM V1), follow the prompt to upload a Firebase service account key. Without this, local notifications work but remote push to the phones does not. (Needs your Firebase project; not done here.)
- Android 13+ asks for notification permission on first launch: allow it.
- The notification small icon currently falls back to the app icon, which Android may render as a plain square. For a polished icon, add a white-on-transparent 96x96 PNG and set `"icon"` in the `expo-notifications` plugin entry in `app.json`.

## Testing voice
1. Install the preview (or development) APK; Expo Go will not work.
2. Open the screen with the mic button, tap it, and allow the Microphone and Speech recognition permission prompts.
3. Say a set, e.g. "bench 60 kilos for 8 reps". The transcript should appear and be parsed.
4. If nothing happens: Android needs the Google speech service (Google app) installed and enabled; check Settings > Apps > AI Coach > Permissions > Microphone.

## What was NOT done
Login, `eas init`, credentials and the actual build need your Expo account and were not run. `app.json` has no `extra.eas.projectId` until you run `eas init`. No build or on-device test has been done.
