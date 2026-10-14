--!strict
--[[
	MovementSimulator — the charged-jump movement model. Pure logic: no Roblox objects, no rendering,
	no networking. One call to `step` advances one fixed time step (see MovementConfig.SimulationHz).

	Coordinates: x grows to the right, y grows upward. (x, y) is the BOTTOM-CENTRE of the body
	(the feet), so `y` is directly the altitude of the feet.

	Modes:
	  Ground    standing / walking on solid ground
	  Charging  on the ground, jump held, power building up (cannot walk)
	  Air       jumping or falling — trajectory is committed (unless air control is enabled)
	  Stunned   short recovery after a long fall ("splat"); no input is accepted

	Collision: the body is moved in small sub-steps, X first then Y. After each axis move,
	overlapping boxes push the body back out along that axis. This gives exact, repeatable
	wall / floor / ceiling contacts. See CollisionWorld for why we do not use Roblox physics.
]]

local Config = require(script.Parent.Parent.Config.MovementConfig)
local CollisionWorld = require(script.Parent.CollisionWorld)

type CollisionWorld = CollisionWorld.CollisionWorld
type Box = CollisionWorld.Box
export type MovementConfig = typeof(Config)

export type Mode = "Ground" | "Charging" | "Air" | "Stunned"

export type Input = {
	moveX: number, -- -1 (left), 0, or 1 (right)
	jumpHeld: boolean,
	cancelHeld: boolean,
}

export type State = {
	x: number,
	y: number,
	vx: number,
	vy: number,
	mode: Mode,
	facing: number, -- -1 or 1
	modeTime: number, -- seconds spent in the current mode
	chargeTime: number,
	lastJumpPower: number, -- 0..1 power of the most recent jump
	stunTimer: number,
	jumpLatched: boolean, -- jump must be released before a new charge may start
	airPeakY: number, -- highest feet altitude during the current airborne phase
	groundKind: string?, -- `kind` of the box currently stood on

	-- Statistics
	highestY: number,
	jumps: number,
	falls: number,
	lastFallDistance: number,
}

export type EventKind =
	"ChargeStart"
	| "ChargeCancel"
	| "Jump" -- value = power 0..1
	| "BumpWall"
	| "BumpCeiling"
	| "Land" -- value = distance fallen from the jump's peak
	| "Fall" -- a landing that counts as a fall (value = distance)
	| "Splat" -- long fall stun (value = distance)
	| "WalkOffEdge"

export type Event = { kind: EventKind, value: number }

-- Tiny gap left between the body and a surface it was pushed out of. Keeps "touching" from
-- counting as "overlapping" despite floating-point rounding.
local SKIN = 1e-4
-- How far below the feet we look for ground support.
local GROUND_PROBE = 0.05

local MovementSimulator = {}

-- Shared scratch list for collision queries (the simulator never runs two steps at once).
local hits: { Box } = {}

local function approach(current: number, target: number, maxDelta: number): number
	if current < target then
		return math.min(current + maxDelta, target)
	else
		return math.max(current - maxDelta, target)
	end
end

local function lerp(a: number, b: number, t: number): number
	return a + (b - a) * t
end

local function setMode(state: State, mode: Mode)
	if state.mode ~= mode then
		state.mode = mode
		state.modeTime = 0
	end
end

local function emit(events: { Event }?, kind: EventKind, value: number?)
	if events then
		table.insert(events, { kind = kind, value = value or 0 })
	end
end

function MovementSimulator.newState(x: number, y: number): State
	return {
		x = x,
		y = y,
		vx = 0,
		vy = 0,
		mode = "Air", -- the first step finds the ground (or starts falling)
		facing = 1,
		modeTime = 0,
		chargeTime = 0,
		lastJumpPower = 0,
		stunTimer = 0,
		jumpLatched = true, -- ignore a jump key that is already held when spawning
		airPeakY = y,
		groundKind = nil,
		highestY = y,
		jumps = 0,
		falls = 0,
		lastFallDistance = 0,
	}
end

-- Places the body at (x, y) and clears its motion (used for respawns and checkpoints).
function MovementSimulator.teleport(state: State, x: number, y: number)
	state.x = x
	state.y = y
	state.vx = 0
	state.vy = 0
	state.chargeTime = 0
	state.stunTimer = 0
	state.airPeakY = y
	state.jumpLatched = true
	setMode(state, "Air")
end

-- 0..1 charge progress while charging (for the HUD / animations).
function MovementSimulator.getChargeRatio(state: State, config: MovementConfig?): number
	if state.mode ~= "Charging" then
		return 0
	end
	local cfg = config or Config
	return math.clamp(state.chargeTime / cfg.MaxChargeTime, 0, 1)
end

-- Returns the box under the feet, if any.
function MovementSimulator.findSupport(state: State, world: CollisionWorld, config: MovementConfig?): Box?
	local cfg = config or Config
	local halfW = cfg.BodyWidth / 2
	local count = world:query(state.x - halfW, state.y - GROUND_PROBE, state.x + halfW, state.y, hits)
	if count == 0 then
		return nil
	end
	-- Prefer the highest surface (relevant only if boxes overlap each other).
	local best = hits[1]
	for i = 2, count do
		if hits[i].maxY > best.maxY then
			best = hits[i]
		end
	end
	return best
end

local function launch(state: State, moveX: number, cfg: MovementConfig, events: { Event }?)
	local t = math.clamp(state.chargeTime / cfg.MaxChargeTime, 0, 1)
	local power = t ^ cfg.ChargeCurveExponent
	state.vy = lerp(cfg.MinJumpVelocityY, cfg.MaxJumpVelocityY, power)
	state.vx = moveX * lerp(cfg.MinJumpVelocityX, cfg.MaxJumpVelocityX, power)
	if moveX ~= 0 then
		state.facing = moveX
	end
	state.chargeTime = 0
	state.lastJumpPower = power
	state.airPeakY = state.y
	state.jumps += 1
	state.groundKind = nil
	setMode(state, "Air")
	emit(events, "Jump", power)
end

local function land(state: State, input: Input, box: Box, cfg: MovementConfig, events: { Event }?)
	local fallDistance = math.max(0, state.airPeakY - state.y)
	state.vy = 0
	if cfg.LandingStopsHorizontal then
		state.vx = 0
	end
	state.groundKind = box.kind
	state.lastFallDistance = fallDistance
	if cfg.RequireJumpReleaseAfterLanding and input.jumpHeld then
		state.jumpLatched = true
	end

	if fallDistance >= cfg.FallCountDistance then
		state.falls += 1
		emit(events, "Fall", fallDistance)
	end
	if cfg.LongFallStunTime > 0 and fallDistance >= cfg.LongFallStunDistance then
		state.vx = 0
		state.stunTimer = cfg.LongFallStunTime
		setMode(state, "Stunned")
		emit(events, "Splat", fallDistance)
	else
		setMode(state, "Ground")
	end
	emit(events, "Land", fallDistance)
end

--[[
	Moves the body by (dx, dy) in sub-steps and resolves collisions.
	Returns the box landed on, if a landing happened during this move.
]]
local function moveAndCollide(state: State, dt: number, world: CollisionWorld, cfg: MovementConfig, events: { Event }?): Box?
	local halfW = cfg.BodyWidth / 2
	local height = cfg.BodyHeight
	local dx = state.vx * dt
	local dy = state.vy * dt
	local steps = math.max(1, math.ceil(math.max(math.abs(dx), math.abs(dy)) / cfg.MaxSubstepDistance))
	local sx = dx / steps
	local sy = dy / steps
	local airborne = state.mode == "Air"
	local landedOn: Box? = nil
	local bumpedWall = false
	local bumpedCeiling = false

	for _ = 1, steps do
		-- ── Horizontal ──
		if sx ~= 0 then
			state.x += sx
			local count = world:query(state.x - halfW, state.y, state.x + halfW, state.y + height, hits)
			if count > 0 then
				-- Ledge forgiveness: while falling, if the only thing hit is a platform whose top
				-- is just above the feet, step onto it instead of bouncing off its corner.
				local snapped = false
				if airborne and state.vy <= 0 and count == 1 then
					local box = hits[1]
					local lift = box.maxY - state.y
					if lift <= cfg.LedgeSnapTolerance then
						local newY = box.maxY + SKIN
						if not world:isBlocked(state.x - halfW, newY, state.x + halfW, newY + height) then
							state.y = newY
							snapped = true
						end
					end
				end

				if not snapped then
					if sx > 0 then
						local wallX = math.huge
						for i = 1, count do
							wallX = math.min(wallX, hits[i].minX)
						end
						state.x = wallX - halfW - SKIN
					else
						local wallX = -math.huge
						for i = 1, count do
							wallX = math.max(wallX, hits[i].maxX)
						end
						state.x = wallX + halfW + SKIN
					end

					if airborne then
						state.vx = -state.vx * cfg.WallBounceFactor
						sx = -sx * cfg.WallBounceFactor
						if state.vx ~= 0 then
							state.facing = if state.vx > 0 then 1 else -1
						end
						bumpedWall = true
					else
						state.vx = 0
						sx = 0
					end
				end
			end
		end

		-- ── Vertical ──
		if sy ~= 0 then
			state.y += sy
			local count = world:query(state.x - halfW, state.y, state.x + halfW, state.y + height, hits)
			if count > 0 then
				if sy < 0 then
					-- Landed: stand on the highest surface we overlapped.
					local top = hits[1]
					for i = 2, count do
						if hits[i].maxY > top.maxY then
							top = hits[i]
						end
					end
					state.y = top.maxY + SKIN
					landedOn = top
					break -- the landing ends this step's movement
				else
					-- Head hit a ceiling: lose all upward momentum.
					local ceiling = math.huge
					for i = 1, count do
						ceiling = math.min(ceiling, hits[i].minY)
					end
					state.y = ceiling - height - SKIN
					state.vy = 0
					state.vx *= cfg.CeilingHorizontalMultiplier
					sx *= cfg.CeilingHorizontalMultiplier
					sy = 0
					bumpedCeiling = true
				end
			end
		end
	end

	if bumpedWall then
		emit(events, "BumpWall")
	end
	if bumpedCeiling then
		emit(events, "BumpCeiling")
	end
	return landedOn
end

--[[
	Advances the simulation by one fixed step.
	`events` (optional) receives what happened this step, for sounds, animations and stats.
]]
function MovementSimulator.step(state: State, input: Input, dt: number, world: CollisionWorld, config: MovementConfig?, events: { Event }?)
	local cfg = config or Config
	local moveX = if input.moveX > 0.5 then 1 elseif input.moveX < -0.5 then -1 else 0

	state.modeTime += dt
	if not input.jumpHeld then
		state.jumpLatched = false
	end

	-- ── Mode logic (decides velocities) ──
	if state.mode == "Stunned" then
		state.vx = 0
		state.stunTimer -= dt
		if input.jumpHeld then
			state.jumpLatched = true
		end
		if state.stunTimer <= 0 then
			setMode(state, "Ground")
		end
	elseif state.mode == "Ground" then
		if input.jumpHeld and not state.jumpLatched then
			state.jumpLatched = true
			state.chargeTime = 0
			if cfg.StopOnChargeStart then
				state.vx = 0
			end
			if moveX ~= 0 then
				state.facing = moveX
			end
			setMode(state, "Charging")
			emit(events, "ChargeStart")
		else
			local target = moveX * cfg.WalkSpeed
			local rate
			if moveX == 0 then
				rate = cfg.GroundDeceleration
			elseif state.vx ~= 0 and (state.vx > 0) ~= (moveX > 0) then
				rate = cfg.TurnAcceleration
			else
				rate = cfg.GroundAcceleration
			end
			state.vx = approach(state.vx, target, rate * dt)
			if moveX ~= 0 then
				state.facing = moveX
			end
		end
	elseif state.mode == "Charging" then
		state.chargeTime += dt
		state.vx = approach(state.vx, 0, cfg.GroundDeceleration * dt)
		if moveX ~= 0 then
			state.facing = moveX
		end
		if cfg.AllowChargeCancel and input.cancelHeld then
			state.chargeTime = 0
			setMode(state, "Ground")
			emit(events, "ChargeCancel")
		elseif not input.jumpHeld or (cfg.AutoJumpAtMaxCharge and state.chargeTime >= cfg.MaxChargeTime) then
			launch(state, moveX, cfg, events)
		end
	elseif state.mode == "Air" then
		if cfg.AirControlAcceleration > 0 and moveX ~= 0 then
			state.vx = approach(state.vx, moveX * cfg.AirControlMaxSpeed, cfg.AirControlAcceleration * dt)
		end
	end

	-- ── Gravity ──
	if state.mode == "Air" then
		state.vy = math.max(state.vy - cfg.Gravity * dt, -cfg.MaxFallSpeed)
	else
		state.vy = 0
	end

	-- ── Move and collide ──
	local landedOn = moveAndCollide(state, dt, world, cfg, events)
	if state.mode == "Air" then
		state.airPeakY = math.max(state.airPeakY, state.y)
		if landedOn then
			land(state, input, landedOn, cfg, events)
		end
	else
		-- Walking (or charging/stunned) on the ground: check we are still supported.
		local support = MovementSimulator.findSupport(state, world, cfg)
		if support then
			state.groundKind = support.kind
		else
			if state.mode == "Charging" then
				state.chargeTime = 0
				emit(events, "ChargeCancel")
			end
			state.vx *= cfg.WalkOffEdgeSpeedMultiplier
			state.airPeakY = state.y
			state.groundKind = nil
			setMode(state, "Air")
			emit(events, "WalkOffEdge")
		end
	end

	state.highestY = math.max(state.highestY, state.y)
end

return MovementSimulator
