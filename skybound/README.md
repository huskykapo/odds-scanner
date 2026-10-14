# SKYBOUND — 2D charged-jump platformer for Roblox

Phase 1 (project foundation) + the first playable slice of Phase 2 (core movement).

You get: a fixed side-view camera, left/right walking, a hold-and-release charged jump with
committed direction, custom wall / floor / ceiling collision, falling with "splat" on long falls,
a placeholder character, other players visible, and a small test level with a climbing route.

---

## 1. One-time setup (Windows)

You need three things: **Rojo** (copies these files into Studio), the **Rojo Studio plugin**, and
an editor. The easiest path for a beginner is VS Code:

1. Install **VS Code**: https://code.visualstudio.com
2. In VS Code, open the Extensions panel and install:
   - **Rojo – Roblox Studio Sync** (by evaera) — it can install Rojo for you and start/stop it
   - **Luau Language Server** (by JohnnyMorganz) — autocomplete and type checking for Luau
3. Get this project on your PC (Git or GitHub Desktop): clone the repository and check out the
   branch `claude/exciting-maxwell-uxhb5p`. Open the **`skybound`** folder in VS Code
   (File → Open Folder…).
4. Install the Rojo **Studio plugin**: in VS Code press `Ctrl+Shift+P` → `Rojo: Open menu` →
   *Install Roblox Studio plugin*. (Alternative: run `rojo plugin install` in a terminal.)
   Restart Roblox Studio afterwards.

<details>
<summary>Command-line alternative (Rokit)</summary>

Install Rokit (https://github.com/rojo-rbx/rokit — download the Windows release, run
`rokit self-install`). Then in the `skybound` folder:

```
rokit install        # installs the exact versions from rokit.toml (Rojo 7.7.1, Lune 0.11.0)
rojo plugin install  # installs the Studio plugin
```
</details>

## 2. Run the game in Roblox Studio

1. Open your place (the "Jump Legend" template place) in Roblox Studio.
2. Start the Rojo server:
   - VS Code: `Ctrl+Shift+P` → `Rojo: Open menu` → click **default.project.json** (Start), or
   - terminal in the `skybound` folder: `rojo serve`
3. In Studio: **Plugins** tab → **Rojo** → **Connect** (address `localhost`, port `34872`).
   Accept the sync. You should now see:
   - `ReplicatedStorage › Skybound` (shared modules)
   - `ServerScriptService › SkyboundServer`
   - `StarterPlayer › StarterPlayerScripts › SkyboundClient`
4. Press **Play** (F5). The Output window should print `[Skybound] Server ready` and
   `[Skybound] Client ready`.
5. While Rojo stays connected, every file you save in VS Code updates in Studio immediately.
   Stop and press Play again to test changes.
6. Save the place in Studio (File → Save / Publish) — Rojo only syncs scripts, the place file
   is still yours.

**What Rojo touches:** only the three folders above. Everything else in your template place
(its map, its own scripts, lighting) is left alone. The Skybound level is built at runtime far
away from the origin (`GameConfig.WorldOrigin`), so it does not overlap the template's map.

**What changes when you press Play:** Roblox avatars are turned off
(`Players.CharacterAutoLoads = false`) because every player uses our own character.
If your template has scripts that expect a Roblox character, they may print errors — tell me what
the template contains and I will adapt.

### Controls

| Action | Keyboard | Gamepad | Touch |
|---|---|---|---|
| Move | A / D or ← / → | Left stick / D-pad | ◀ ▶ buttons |
| Charge, release to jump | hold Space (or W / ↑) | hold A | hold JUMP |
| Cancel charge | S / ↓ | B | — |
| Restart at the start | R (or Roblox menu → Reset) | — | Roblox menu → Reset |
| Show/hide debug info | F1 | — | — |

The direction you hold **when you release** jump decides the jump direction. Once in the air the
trajectory is committed (no steering).

### Things to test

- Tap vs. full charge (the yellow bar at the bottom shows the charge).
- Jump into the **low ceiling** in the middle: you bonk and drop.
- Jump into walls: you rebound at half speed.
- Stand on the 1-stud **narrow post**.
- Climb the route Step1 → Step7 → gold **Summit**, then walk off the right side of the
  Summit for a long fall into the pit ("Ouch!").
- Test with **Play** and also **Test → Clients and Servers** with 2 players to see each other.
- Device emulator (Test → Device) for phone/tablet screens and touch buttons.
- Frame rate: Studio → Settings → Rendering → *Frame Rate Manager* / or Roblox's FPS cap in the
  game menu; the jump should land in the same place at any FPS.

---

## 3. Tuning the movement

All movement numbers are in **`src/shared/Config/MovementConfig.luau`**, each with a comment.
Camera numbers are in **`CameraConfig.luau`**, the rest in **`GameConfig.luau`**.
Save the file → Rojo syncs it → press Play again.

The most important knobs:

| Setting | Effect |
|---|---|
| `MaxJumpVelocityY`, `Gravity` | max jump height = Vy² / (2·Gravity) |
| `MinJumpVelocityX/MaxJumpVelocityX` | sideways jump speed → jump distance |
| `MaxChargeTime` | how long a full charge takes |
| `ChargeCurveExponent` | >1 gives finer control over small hops |
| `WallBounceFactor` | 0 = stop at walls, 0.5 = rebound at half speed |
| `AirControlAcceleration` | 0 = committed jumps; >0 allows mid-air steering |
| `LongFallStunDistance/Time` | when and how long the "splat" happens |
| `CameraConfig.VerticalMode` | `"Follow"` (smooth) or `"Screens"` (screen-by-screen) |

After changing movement values, run the automated tests (next section): they check that every
jump on the test level's route is still possible.

## 4. Automated tests (no Studio needed)

Requires Lune (installed by `rokit install`). From the `skybound` folder:

```
lune run tests/run       # 29 movement & level tests
lune run tests/compile   # syntax check of every source file
```

`tests/run` drives the real `MovementSimulator` and `CollisionWorld` modules: jump heights,
charge, ceilings, wall rebound, tunneling, corner landings, splat, determinism, and a search
that proves each step of the test level's climbing route is reachable.
They do **not** test rendering, input, camera or networking — those need Studio play-testing.

---

## 5. Project structure

```
skybound/
  default.project.json        Rojo: maps src/ folders into the Roblox game
  rokit.toml                  tool versions (Rojo, Lune)
  selene.toml                 linter config (run `selene generate-roblox-std` once, then `selene src`)
  src/
    shared/   → ReplicatedStorage.Skybound           (used by server AND client)
      Config/GameConfig, MovementConfig, CameraConfig
      Movement/CollisionWorld        2D box collision + spatial grid
      Movement/MovementSimulator     the charged-jump movement model (pure logic)
      Levels/LevelGeometry           level data → collision world
      Levels/TestLevel               the movement test room
      Net                            remote events + compact binary state format
    server/   → ServerScriptService.SkyboundServer (Script)
      init.server.luau               startup
      LevelBuilder                   builds the visible level Parts
      PlayerStateService             receives/validates positions, broadcasts, respawns
    client/   → StarterPlayerScripts.SkyboundClient (LocalScript)
      init.client.luau               per-frame loop
      InputController                keyboard / gamepad / touch
      MovementController             fixed-timestep runner + smooth interpolation
      CameraController               side-view camera
      CharacterRenderer              placeholder character (sprites in Phase 3)
      RemotePlayers                  other players
      AudioController                movement sounds
      DebugHUD                       stats / charge meter / messages
  tests/                             Lune tests (not synced into Roblox)
  docs/TECHNICAL_DECISIONS.md        why things are built this way
```

## 6. What is placeholder right now

- Character: a coloured block with an eye (no sprites yet).
- Level art: flat coloured blocks + a sky-coloured backdrop.
- Sounds: Roblox's built-in default jump/land sounds.
- HUD: a debug overlay, not the final UI. No menus.
- Server checks only log suspicious movement; nothing is enforced yet.
- No data saving, no modes, no checkpoints yet.
