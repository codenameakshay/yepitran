# yepitran

**Yep, it ran.** yepitran proves a mobile app works on a real iOS Simulator and a real Android
Emulator. Give it your app and a short scenario. It gives you a report, screenshots, a video when recording succeeds, a
pass or fail for every step, and one verdict:

| Verdict | Meaning | Exit code |
|---|---|---|
| `PASS` | Every step passed on a real device. | 0 |
| `APP FAIL` | Two device attempts report app failures, or a Flutter build reports a recognized source or dependency error. | 1 |
| `INFRA FAIL` | The device or the tooling broke. This says nothing about your app. The report lists what yepitran tried. | 2 |

The rule it is built around: **a flaky device is never blamed on your app.** Every attempt
starts on a cleanly booted device. Device runs count against the app only after two app-failure attempts.
Build errors do not need a device retry. Simulators that hang, adb servers that die and emulators that crash are
recovered, or reported as `INFRA FAIL`.

It is plain Python 3.10+ with no dependencies. It drives `xcrun simctl`,
[AXe](https://github.com/cameroncooke/AXe), `adb` and the Android emulator. It works well with
coding agents: they write the scenario, run it, and read `verdict.json`.

## Install

```sh
pipx install git+https://github.com/codenameakshay/yepitran     # or: uv tool install git+https://github.com/codenameakshay/yepitran
yepitran doctor                                                # checks your machine; --fix applies the safe fixes
```

What it needs:

- **iOS:** macOS, Xcode with an iOS simulator runtime, and AXe (`brew install cameroncooke/axe/axe`).
- **Android:** the Android SDK (command-line tools under `cmdline-tools/latest`, platform-tools, build-tools, emulator and an API 36 system
  image) on macOS or Linux, or a [Simlock](https://github.com/callstackincubator/simlock) gateway (see below).
- Local Android also needs a JDK, `lsof` and `ps`. Linux needs hardware virtualization and access to `/dev/kvm`.
- `ffmpeg` converts Android recordings to MP4. Without it, recordings stay WebM. Flutter is needed only for automatic builds.

The SDK location is `ANDROID_HOME`, then `ANDROID_SDK_ROOT`, then `~/Library/Android/sdk` on macOS or `~/Android/Sdk` on Linux.
The local image is `system-images;android-36;google_apis;arm64-v8a` on ARM64 hosts or `system-images;android-36;google_apis;x86_64` on x86_64 hosts.
`ANDROID_AVD_HOME` and `ANDROID_USER_HOME` control the AVD directory. An explicit `JAVA_HOME` selects the JDK.
Fleet runs still need local SDK build-tools to inspect APKs, but do not need a local emulator or KVM.

## Try it in 5 minutes

The repo has a small demo app (`examples/demo`, a Flutter to-do list) and a scenario that
proves you can add a task:

```sh
git clone https://github.com/codenameakshay/yepitran && cd yepitran
yepitran examples/demo examples/demo/demo.proof
```

This builds and runs both platforms on macOS, or Android only on Linux. Open the printed `report.html` path in a browser.

```
launch
wait "Yep Todo"
tap "Add task"
type "Try yepitran" into "Task title"
tap "Save"
assert gone "New task"
assert visible "Try yepitran"
screenshot task-added
```

To skip the build, pass built artifacts: `yepitran Runner.app app.apk my.proof`.

## In CI: the GitHub Action

Prove every pull request on real devices. GitHub's macOS and Linux runners are free for public repos.

```yaml
jobs:
  ios:
    runs-on: macos-15
    permissions: { contents: read, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
      - run: flutter build ios --simulator --debug
      - uses: codenameakshay/yepitran@v0
        with:
          platform: ios
          app: build/ios/iphonesimulator/Runner.app
          scenario: .yepitran/smoke.proof
          comment: true          # post the verdict on the PR
  android:
    runs-on: ubuntu-latest       # hosted macOS runners cannot run the Android emulator
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
      - run: flutter build apk --profile --target-platform android-arm64,android-x64
      - uses: codenameakshay/yepitran@v0
        with:
          platform: android
          app: build/app/outputs/flutter-apk/app-profile.apk
          scenario: .yepitran/smoke.proof
```

The report and available video and screenshots are uploaded as workflow artifacts, including a fallback report if setup fails. The step fails unless
the verdict is `PASS`. The action's outputs are `verdict` and `report`.
PR comments require `pull-requests: write` and the hosted runner's `gh` CLI. Fork PRs and Dependabot runs skip comments because their tokens are read-only.
For fleet Android, pass both `simlock-url` and `simlock-token`. Local Android requires an x86_64 Linux runner with KVM.
Install Flutter before the action if `app` is a Flutter project directory.

## With a coding agent

`skills/yepitran/SKILL.md` teaches Claude Code, Codex and similar agents to write a scenario
from a change, run it, and read the verdict. Copy it into your agent's skills folder (for
Claude Code: `~/.claude/skills/yepitran/`).

## Commands

```
yepitran <app.app|app.apk>...       <scenario> [--platform ios|android|both] [--out DIR]
yepitran <flutter project>         <scenario> [--platform ios|android|both]
yepitran --version
yepitran doctor [--fix]
yepitran pr <https://github.com/owner/repo/pull/N> [--scenario FILE] [--platform ...] [--post]
```

Flutter projects default to both platforms on macOS and Android elsewhere. `--platform both` uses that same host-specific set.
It does not prove iOS on Linux. Use `--platform ios` or `--platform android` to select one platform explicitly.
Bad usage exits with code 64. `doctor` returns 0 when no check fails, 1 for failed checks, and 2 if an error prevents completion.

`yepitran pr` clones the PR head into `~/.yepitran/pr/`, builds it, runs the scenario, and
prints the PR comment. It posts nothing unless you pass `--post`. The scenario comes from
`--scenario`, else a fenced ` ```yepitran ` block in the PR body, else `.yepitran/*.proof` in the repo.

## Scenario files

One step per line. An agent can write one from a PR description.

```
# headers come first
app: com.example.app
permissions: allow
reverse: 3000
timeout: 20s

launch
wait "Welcome" 30s
tap "Sign in"
tap id=login_button
tap has="Sign"
tap "Add task" #2
type "hello" into "Name"
scroll down until "Item 40"
deeplink myapp://settings
assert visible "Saved"
assert gone "Spinner" 5s
screenshot after-save
ios: tap "Done"
android: tap "OK"
```

Headers are optional. `app:` checks the artifact's package or bundle ID. `permissions:` accepts `allow` or `deny`.
`reverse:` keeps the listed Android TCP ports forwarded. `timeout:` sets the default wait budget.
`id=` matches accessibility identifiers or resource IDs. `has=` matches substrings. `#2` selects the second match, top to bottom.
Typing into a field taps it, enters text, and reads the value back. Prefix steps with `ios:` or `android:` for platform-only steps.
Only whole-line comments are supported. Explanatory text after a command or header is not a comment.

Selectors match what the accessibility tree says. When a step fails, the report lists what
was on screen, and `NN-failure.tree.txt` in the attempt folder has the whole tree. A tap that
matches more than one element logs a note telling you to add `#N`.

`assert visible` also matches a text field that contains the text. To prove a form saved,
assert the form is gone first (see `examples/demo/demo.proof`).

## The report folder

```
yepitran-out/<time>-<scenario>-<random-id>/
  report.html  report.md  verdict.json  scenario.txt
  ios/yepitran.log
  ios/attempt-1/video.mp4  03-home.png  14-failure.png  14-failure.tree.txt
  android/attempt-1/video.mp4  logcat.txt (on app failure)  emulator.log
```

`verdict.json` is for agents. `report.md` is the PR comment. `report.html` is for people.
PASS describes the scenario steps, not the presence of a recording. A recorder failure can leave a passing run without video.

## How it stays reliable

**Its own devices.** yepitran creates and uses only `yepitran iPhone` (an iPhone Pro on the
newest runtime that has one; foldables give AXe an empty tree), and on Android either the AVD
`yepitran_API36` or a device leased from Simlock. It never touches another session's simulator
or emulator.

**Leases.** One `flock` per platform in `~/.yepitran/locks/`. A second run on the same
platform waits in a queue and says who holds the device. The kernel frees the lock when a run
dies, even on `kill -9`. yepitran uses at most two devices: one iOS, one Android.
`YEPITRAN_HOME` overrides `~/.yepitran` for locks, PR clones and the Simlock config.

**Clean boot every attempt.** iOS: shut down, write keyboard settings, boot, uninstall,
reset the keychain (it survives an uninstall), install, grant or revoke privacy. Android:
boot from a clean snapshot, uninstall, install with `-g`. Attempt 2 cold-boots. Attempt 3
erases or wipes the device.

**Cleanup.** Ctrl-C and SIGTERM stop the video, shut the devices down, and release the
leases. After a crash or `kill -9`, the next run (or `doctor --fix`) kills that run's
leftover processes. It only kills tool processes (`axe`, `simctl`, `qemu`, `adb`, ...) that
name a yepitran device, and only while it holds that device's lease.

**Timeouts follow machine load.** At load 90 on 18 cores every wait stretches up to 5x.

### Device traps it handles

| Trap | Handling |
|---|---|
| AXe 1.7 cannot load SimulatorKit on Xcode 27.1 | `doctor` checks AXe >= 1.8.0 (`--fix` upgrades). A run says so plainly instead of timing out on a wait. |
| AXe fails for ~30 s after boot ("remote automation session") | Waits for the first good tree before installing; retries transient AXe errors. |
| `axe type` puts the sim in hardware-keyboard mode; autocorrect rewrites typed text ("yepitran" became "yep it ran") | Text is pasted (`simctl pbcopy` + Cmd+V), then read back. Words split by a space count as a failed entry. Every attempt boots fresh, so keyboard mode never leaks between runs. |
| "Allow Paste", notification and permission prompts | A table of system dialogs, answered by the `permissions:` policy whenever the app's element is not found yet. |
| iPhone 17 Pro not offered on iOS 27.1; iPhone Duo is a foldable | Picks the newest runtime that offers a plain iPhone Pro. |
| iOS keychain survives uninstall | `simctl keychain reset` before every install. |
| Off-screen rows listed with a 0x0 frame | Elements must be on screen to match. |
| A centre tap lands under a sheet, tab bar or overlay | Taps the point of the element no other labelled element covers. |
| Cold deep link bounces to Safari | Checks the app is in front; relaunches warm and resends, up to 3 times. |
| Shared emulators and port collisions | Own AVD on ports 5700+, outside the default adb scan, guarded by checking `avd name` on its console. |
| Other sessions' `adb kill-server`; `adb reverse` dies | Private adb server on port 5137 that scans nothing. Port 5137 is yepitran's: any adb server on it (a dead run's, or one adb auto-started) is stopped and restarted with the scan limit; a non-adb process there is reported, never killed. When adb drops, yepitran restarts it, waits for the emulator to re-register, and re-applies every `reverse:` port. A tap whose adb call died is not replayed (it may have landed); the attempt is retried instead. |
| Truncated `screencap` PNGs | Every PNG is checked for its end marker; 3 retries, then the emulator's own framebuffer. |
| Zygote / system_server restart | Waits for a stable runtime after boot. A restart mid-run is an infra failure, never an app failure. |
| TalkBack turns taps into focus moves | Turned off at boot and after any step that finds it on. |
| Debug APKs ANR under `uiautomator dump` | Flutter projects build Android as `--profile`. |
| Swiftshader ANRs on macOS | The emulator uses `-gpu host` on macOS and Swiftshader on headless Linux (`YEPITRAN_EMULATOR_GPU` overrides it). |
| "isn't responding" / "keeps stopping" dialogs, heads-up notifications | Dialogs answered (Wait / Close app). Heads-up notifications and keyguard are off. |
| Permission re-prompts (USER_FIXED) | Fresh install every attempt with `-g`, or no grants under `permissions: deny`. |
| Orphaned processes holding a device | Reaped by the next lease holder; `doctor` lists and `--fix` kills them. |
| Video recorder limits | iOS: `simctl io recordVideo`. Android: emulator console recording (survives adb restarts), rotated every 170 s and joined with ffmpeg. |

**Is the app still in front?** After every step yepitran checks the app is running and in
front (allowing a few seconds for transitions). A crash, even on the last step, is APP FAIL
with the crash report. On Android, selectors only match the app's own package, so a system
screen with the same text cannot pass a step.

**Whose fault is a miss?** When an element never shows up, yepitran checks in order: is the
app running (no: APP FAIL with the crash report or logcat crash buffer), is it in front (no:
INFRA FAIL naming what is in front), does the tree show anything from the app (no: INFRA
FAIL), otherwise APP FAIL with the list of what is on screen. A device that fails its health
check after a step turns that step's failure into an infra failure.

## Android on simlock

When yepitran finds a Simlock gateway, Android runs on a fleet emulator instead of one on this
machine. iOS still runs on a local simulator.

Setup, once. Create an agent token on the gateway, then save it:

```sh
ssh <gateway host> simlock --gateway token create --role agent --label yepitran
# put the URL and the secret in ~/.yepitran/simlock.json (mode 600):
#   {"url": "https://<gateway>", "token": "slk_..."}
# or set YEPITRAN_SIMLOCK_URL and YEPITRAN_SIMLOCK_TOKEN
yepitran doctor     # shows "Android devices: simlock fleet at ..."
```

How it works:

- Every attempt leases a fresh `Pixel 8` (`YEPITRAN_ANDROID_DEVICE` picks another model from
  `simlock catalog`). Simlock wipes a device when its lease ends, so a retry always starts clean.
- adb runs on the worker that holds the device, through the gateway's `exec` API. Simlock adds
  the device serial and refuses any other device, so yepitran cannot touch another agent's device.
- The APK is uploaded in base64 parts, 4 at a time, and checked with md5 on the device
  (a 67 MB APK took 1 to 3 minutes over a VPN). Screenshots and video come back the same way,
  so a fleet run of the example takes about 4 minutes against 1.5 minutes on a local emulator.
- The lease is renewed every 2.5 minutes. Ctrl-C gives it back. After a `kill -9` it expires in
  10 minutes, or the next run gives it back first.
- A lease that simlock takes back mid-run is an infra failure, and the next attempt leases a new device.
- Only a Simlock error answer (the command never ran) is resent. A broken stream or a proxy error
  means the command may have run, so a tap is never sent twice; the attempt is retried instead.
- `YEPITRAN_ANDROID=local` uses a local emulator instead.

Limits on the fleet: `reverse:` is refused (adb reverse would reach the worker, not this machine), and
each step is about 1 s slower because every adb call crosses the network.

## Limits

- Flutter projects build automatically. A native Xcode or Gradle app works when you pass its
  built `.app` (simulator build) or `.apk`.
- iOS typing pastes text, so it cannot test per-key behaviour such as input masks that react
  to each keystroke.
- Android uses a hardware keyboard profile (`hw.keyboard=yes`), so the soft keyboard never
  covers the screen. Keyboard-layout behaviour needs a manual check.
- Scenario steps cannot tap system UI on Android (only the app's package matches); known
  system dialogs are answered by the `permissions:` policy.
- `yepitran pr` comments carry the verdict and step table. Video and screenshots stay on
  the machine that ran it.

## Development

```sh
YEPITRAN_HOME=$(mktemp -d) python3 -m unittest discover -s tests      # unit tests, no devices needed
python3 scripts/soak.py  Runner.app app.apk my.proof -n 10             # 10 real runs per platform
python3 scripts/chaos.py Runner.app app.apk my.proof                   # break things on purpose
```

`bin/yepitran` runs the CLI from a checkout.

## License

MIT
