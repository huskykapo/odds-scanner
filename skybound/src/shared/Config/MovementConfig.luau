--!strict
--[[
	MovementConfig — every number that shapes how the character moves.

	Units:
	  * distance  = studs (1 stud is the Roblox grid unit; our placeholder body is 2 x 3 studs)
	  * time      = seconds
	  * speed     = studs per second
	  * accel     = studs per second, per second

	All characters/skins use these SAME values. Skins are visual only.

	Quick reference, derived from the defaults below (useful when designing levels):
	  * Highest jump (full charge, straight up):  MaxJumpVelocityY^2 / (2 * Gravity) ~= 9.6 studs
	  * Lowest jump  (tap):                       MinJumpVelocityY^2 / (2 * Gravity) ~= 1.2 studs
	  * Air time of a full jump back to the same height: 2 * MaxJumpVelocityY / Gravity ~= 0.84 s
	  * Horizontal distance of that full jump:   ~= 0.84 s * 13 = ~10.9 studs
	Run `lune run tests/run` (see skybound/README.md) after changing values: the tests check that
	the test level is still climbable with the new numbers.
]]

local MovementConfig = {
	-- ── Simulation ────────────────────────────────────────────────────────────
	-- The movement simulation runs at a fixed rate, independent of the player's frame rate,
	-- so a jump lands in the same place at 30 FPS and at 240 FPS.
	SimulationHz = 120,
	-- Safety cap: if the game freezes for a moment we simulate at most this many steps in one frame
	-- (prevents a "spiral of death" where catching up takes longer than the time it covers).
	MaxStepsPerFrame = 12,
	-- Each step's movement is split into small sub-moves no longer than this, so a fast-falling
	-- character can never pass through a thin platform ("tunneling"). Keep it below the
	-- thinnest platform you build (platforms should be at least 0.5 studs thick).
	MaxSubstepDistance = 0.25,

	-- ── Body (collision box) ──────────────────────────────────────────────────
	-- Identical for every character skin.
	BodyWidth = 2,
	BodyHeight = 3,

	-- ── Ground movement ───────────────────────────────────────────────────────
	WalkSpeed = 11,
	-- How fast you reach WalkSpeed from standing still.
	GroundAcceleration = 140,
	-- How fast you stop when no direction is held (this is effectively ground friction).
	GroundDeceleration = 180,
	-- Acceleration used when reversing direction (higher = snappier turns).
	TurnAcceleration = 260,

	-- ── Jump charging ─────────────────────────────────────────────────────────
	-- Holding jump for this long gives a full-power jump.
	MaxChargeTime = 0.6,
	-- true: at full charge the character jumps by itself (you cannot "hold" a full charge forever).
	AutoJumpAtMaxCharge = true,
	-- true: the character stops walking as soon as charging starts (classic charged-jump feel).
	StopOnChargeStart = true,
	-- true: pressing the cancel input (S / Down / gamepad B) while charging cancels the jump.
	AllowChargeCancel = true,
	-- true: if jump is still held when you land, you must release and press again to charge.
	-- Prevents accidental full-power jumps right after landing.
	RequireJumpReleaseAfterLanding = true,

	-- Charge -> launch velocity mapping.
	--   power = (chargeTime / MaxChargeTime) ^ ChargeCurveExponent       (0..1)
	--   vy    = lerp(MinJumpVelocityY, MaxJumpVelocityY, power)
	--   vx    = direction * lerp(MinJumpVelocityX, MaxJumpVelocityX, power)
	-- Exponent 1 = linear. Above 1 = more control over small hops, below 1 = big jumps come sooner.
	ChargeCurveExponent = 1,
	MinJumpVelocityY = 16,
	MaxJumpVelocityY = 46,
	-- Equal min/max horizontal speed means distance is controlled purely by how long you are in
	-- the air (i.e. by charge). Make them differ if stronger jumps should also be faster sideways.
	MinJumpVelocityX = 13,
	MaxJumpVelocityX = 13,

	-- ── Air ───────────────────────────────────────────────────────────────────
	Gravity = 110,
	MaxFallSpeed = 70,
	-- Mid-air steering. 0 = fully committed trajectory (default, highest skill ceiling).
	-- Try e.g. 20 for slight correction.
	AirControlAcceleration = 0,
	AirControlMaxSpeed = 6,
	-- Horizontal speed kept when walking off a ledge (1 = keep walking speed, 0 = drop straight down).
	WalkOffEdgeSpeedMultiplier = 0.5,

	-- ── Collision responses ───────────────────────────────────────────────────
	-- Hitting a wall in the air: horizontal speed is reversed and multiplied by this.
	-- 0 = stop dead and slide down the wall, 0.5 = rebound at half speed.
	WallBounceFactor = 0.5,
	-- Hitting a ceiling: upward speed becomes 0 (momentum is lost). Horizontal speed is multiplied by this.
	CeilingHorizontalMultiplier = 1,
	-- true: landing stops all horizontal motion (no sliding after a jump).
	LandingStopsHorizontal = true,
	-- When the side of the body clips the top corner of a platform while falling, and the overlap
	-- is at most this tall, the body is placed on top of the platform instead of bouncing off the
	-- corner. Should be about MaxSubstepDistance; set 0 to disable.
	LedgeSnapTolerance = 0.3,

	-- ── Falling ───────────────────────────────────────────────────────────────
	-- A landing at least this far below the highest point of the jump counts as a "fall" in stats.
	FallCountDistance = 12,
	-- A landing at least this far below the jump's peak makes the character "splat" briefly.
	LongFallStunDistance = 24,
	LongFallStunTime = 0.45,
}

return MovementConfig
