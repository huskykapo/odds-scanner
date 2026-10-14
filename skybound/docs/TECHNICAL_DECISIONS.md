# Technical decisions (and Roblox limitations)

## 1. Movement: custom kinematic controller, not Roblox physics

**Options considered**

| Approach | Pros | Cons |
|---|---|---|
| Roblox Humanoid + default jump | zero work | no charged jump, floaty, frame-rate and physics-dependent, 3D |
| Humanoid/physics + scripted impulses | uses engine collision | impulses and friction vary; tunneling and bouncing are hard to control; network ownership quirks |
| Shape casts (`Workspace:Blockcast`) each frame | engine queries, works with any Part | casts miss parts already overlapping at the start; float precision near corners; harder to unit test |
| **Own 2D box collision (chosen)** | exact and repeatable, same result on client and server, unit-testable outside Studio, cheap | level collision must be axis-aligned rectangles (no slopes yet) |

**How it works:** `MovementSimulator` runs at a fixed 120 steps/second. Each step's motion is split
into sub-moves of at most 0.25 studs (no tunneling through thin platforms). The body moves on X,
is pushed out of anything it overlaps, then on Y the same way. That gives clean wall, floor and
ceiling contacts. A small "ledge snap" rule lands you on a platform when your side clips its top
corner while falling, instead of bouncing off the corner.

**Frame-rate independence:** the simulation always uses the same step size; frames just decide
how many steps to run. Visual position is interpolated between steps so 144 Hz screens look
smooth. Remaining limitation: input is sampled once per frame, so at 30 FPS the charge time is
measured in 1/30 s increments (at 60 FPS, 1/60 s).

**Slopes:** Jump King-style sloped surfaces are not supported yet. They can be added later as a
new block shape in `CollisionWorld` without changing the rest of the game.

## 2. No Roblox avatar

`Players.CharacterAutoLoads` is off. Each client simulates its own body and draws it with
`CharacterRenderer`. This guarantees identical physics for every skin and keeps Roblox avatar
scale/accessories out of gameplay. Consequences handled in code:
- Streaming: the server moves an invisible `ReplicationFocus` part with each player.
- The Roblox "Reset Character" button is redirected to "restart level".

## 3. Camera: approximated orthographic view

Roblox has **no orthographic camera**. We use a scriptable perspective camera with a narrow field
of view (20°) placed ~140 studs away. Blocks are only 4 studs deep and characters are drawn just
in front of them, so perspective effects are a fraction of a stud. The distance is computed from
the screen shape each frame: the same height of level is visible on every device, and portrait
phones zoom out to keep a minimum width.

## 4. Networking and integrity

- Client → server: 19-byte binary state 20×/s over an `UnreliableRemoteEvent` (newest packet
  matters; lost packets are fine).
- Server → clients: one batched snapshot 15×/s (19 bytes per player, chunked under the
  ~900-byte unreliable payload limit).
- The server checks each report: max plausible speed (with tolerance for network jitter) and
  "not inside solid geometry" using the same collision data as the client. Suspicious reports
  are excluded from the highest-altitude record and logged.

**Not yet:** server re-simulation of inputs, position correction, kicking, or protection of
rewards (rewards do not exist yet). Planned before the competitive tower mode.

## 5. Level data instead of Parts for collision

Collision is built from the level's data table on both client and server. Visible Parts are made
from the same data. Benefits: no dependence on which Parts have streamed in, the server can validate
positions, and tests can load the level. Planned next: a Studio workflow where you build levels
with Parts (or Blender models) and a plugin/script exports them to level data.

## 6. Roblox limitations to keep in mind

- No native orthographic camera (see 3).
- `UnreliableRemoteEvent` payloads are limited (~900 bytes).
- Client input is only read once per rendered frame.
- `SetCore("ResetButtonCallback")` can fail until core scripts load; we retry.
- Lighting/fog settings of the template place can tint or fog the view (camera is ~140 studs away).
