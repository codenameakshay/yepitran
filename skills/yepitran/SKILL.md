---
name: yepitran
description: Prove a mobile app change works on a real iOS Simulator or Android Emulator with yepitran, and bring back a video, screenshots and a PASS / APP FAIL / INFRA FAIL verdict. Use when asked to prove, verify, demo or check a mobile (Flutter, iOS or Android) change on a device, or to add device proof to a PR.
---

# Prove a mobile change with yepitran

yepitran installs the app on its own clean simulator or emulator, runs a short scenario,
records a video when the recorder works, and returns one verdict. It never touches another session's device.

## 1. Check the machine once

```sh
yepitran doctor        # every line that is not "ok" names its fix; `yepitran doctor --fix` applies the safe ones
```

iOS needs macOS with Xcode and AXe. Android needs a local emulator, or a Simlock gateway
(`YEPITRAN_SIMLOCK_URL` and `YEPITRAN_SIMLOCK_TOKEN`).
Local Android needs SDK command-line tools, platform-tools, build-tools, a host-ABI API 36 image, a JDK, `lsof` and `ps`.
Linux also needs KVM access. Fleet runs need local SDK build-tools for APK inspection.

## 2. Write the scenario

Write a `.proof` file that walks through what the change claims to do. One step per line:

```
app: com.example.app
launch
wait "Welcome"
tap "Sign in"
type "ada@example.com" into "Email"
tap "Continue"
assert gone "Continue"
assert visible "Hi Ada"
screenshot signed-in
```

Steps: `launch`, `wait "X" [30s]`, `tap "X"` (exact text), `tap id=X`, `tap has="X"`,
`tap "X" #2`, `type "text" into "Field"`, `scroll down until "X"`, `deeplink url`,
`assert visible "X"`, `assert gone "X" [5s]`, `screenshot name`. Prefix a step with `ios:` or
`android:` when the platforms differ. Headers: `app:`, `permissions: allow|deny`, `timeout: 20s`.

Rules that keep a scenario honest:

- Match text the user sees. Take it from the change or the screen, not from the code's variable names.
- Prove the outcome, not the click: after a save, assert the form is gone, then assert the new data is visible.
- Take a `screenshot` at each moment a reviewer would want to see.

## 3. Run it

```sh
yepitran path/to/Runner.app path/to/app.apk change.proof          # built artifacts
yepitran path/to/flutter_project change.proof --platform both      # builds the project first
```

On Linux, the Flutter default and `--platform both` run Android only. They do not prove iOS.
Run exit codes: 0 PASS, 1 APP FAIL, 2 INFRA FAIL, 64 bad usage.
`doctor` returns 0 if no check fails, 1 for failed checks, and 2 if an error prevents completion.
The report folder is printed at the end of a completed run. Usage errors can exit before any report exists.

## 4. Read the verdict

Read `verdict.json` in the report folder. Do not judge the run by reading terminal output.

- **PASS**: every step passed on the platforms listed in `legs`. Share `report.md` and available linked evidence. PASS does not guarantee a video.
- **APP FAIL**: two device attempts report app failures, or a Flutter build reports a recognized source or dependency error.
  For a failed scenario step, its `message` can list what was on screen; `NN-failure.tree.txt` holds the UI tree when capture succeeds.
  Fix the app, or the scenario if its text was wrong, then run again.
- **INFRA FAIL**: the device or the tooling broke. This says nothing about the app. Run
  `yepitran doctor`, fix what it reports, and run again. Do not change app code because of an INFRA FAIL.

Never report a change as proven unless the verdict is PASS. Look at the final screenshot
yourself before you say it shows the right thing.
