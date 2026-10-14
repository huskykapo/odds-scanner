--[[
	Headless tests for the movement core. Run from the `skybound` folder:

	    lune run tests/run

	They exercise the same MovementSimulator / CollisionWorld code that runs in Roblox, with no
	Roblox Studio needed. They do NOT test rendering, camera, input or networking.
]]

local process = require("@lune/process")
local Shim = require("./RobloxShim")

local Config = Shim.require("src/shared/Config/MovementConfig.luau")
local CollisionWorld = Shim.require("src/shared/Movement/CollisionWorld.luau")
local Sim = Shim.require("src/shared/Movement/MovementSimulator.luau")
local LevelGeometry = Shim.require("src/shared/Levels/LevelGeometry.luau")
local TestLevel = Shim.require("src/shared/Levels/TestLevel.luau")

local DT = 1 / Config.SimulationHz
local passed, failed = 0, 0

local function test(name: string, fn: () -> ())
	local ok, err = pcall(fn)
	if ok then
		passed += 1
		print("  ok    " .. name)
	else
		failed += 1
		print("  FAIL  " .. name .. "\n        " .. tostring(err))
	end
end

local function check(condition: boolean, message: string)
	if not condition then
		error(message, 2)
	end
end

local function near(a: number, b: number, tolerance: number, message: string)
	if math.abs(a - b) > tolerance then
		error(("%s: expected %.4f, got %.4f (tolerance %.4f)"):format(message, b, a, tolerance), 2)
	end
end

local function input(moveX: number?, jump: boolean?, cancel: boolean?)
	return { moveX = moveX or 0, jumpHeld = jump == true, cancelHeld = cancel == true }
end

local IDLE = input()

-- Runs `steps` simulation steps with a fixed input; returns all events.
local function run(state, world, inp, steps: number, events)
	events = events or {}
	for _ = 1, steps do
		Sim.step(state, inp, DT, world, Config, events)
	end
	return events
end

local function hasEvent(events, kind: string): boolean
	for _, e in events do
		if e.kind == kind then
			return true
		end
	end
	return false
end

-- Steps until `done(state)` is true or `maxSeconds` pass. Returns true if done.
local function runUntil(state, world, inp, maxSeconds: number, done, events): boolean
	for _ = 1, math.ceil(maxSeconds / DT) do
		Sim.step(state, inp, DT, world, Config, events)
		if done(state) then
			return true
		end
	end
	return false
end

-- Spawns a body standing on a flat floor (top at y = 0) and returns it.
local function flatWorld()
	local world = CollisionWorld.new()
	world:addBox(-100, -10, 100, 0, "Ground")
	return world
end

local function standingAt(world, x: number, y: number)
	local state = Sim.newState(x, y)
	runUntil(state, world, IDLE, 2, function(st)
		return st.mode == "Ground"
	end)
	assert(state.mode == "Ground", "body did not settle on the ground")
	return state
end

-- Holds jump for `chargeSteps` steps, then releases with direction `dir`. Returns events.
local function chargedJump(state, world, chargeSteps: number, dir: number, events)
	events = events or {}
	run(state, world, input(dir, true), chargeSteps, events)
	if state.mode == "Charging" then
		run(state, world, input(dir, false), 1, events)
	end
	return events
end

print("CollisionWorld")

test("touching boxes do not overlap; overlapping ones do", function()
	local world = CollisionWorld.new()
	world:addBox(0, 0, 10, 1)
	check(not world:isBlocked(0, 1, 10, 2), "box resting on top must not overlap")
	check(not world:isBlocked(10, 0, 12, 1), "box beside must not overlap")
	check(world:isBlocked(9.99, 0.5, 12, 2), "corner overlap must be found")
end)

test("query finds boxes spanning several grid cells exactly once", function()
	local world = CollisionWorld.new(4)
	world:addBox(-20, -20, 20, 20)
	local out = {}
	check(world:query(-30, -30, 30, 30, out) == 1, "expected one result")
end)

print("Ground movement")

test("spawn settles on the floor", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 2)
	near(s.y, 0, 0.001, "feet height")
	check(s.vy == 0, "vertical speed should be 0 on ground")
end)

test("walking reaches WalkSpeed and stops when released", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	run(s, world, input(1), math.ceil(0.5 / DT))
	near(s.vx, Config.WalkSpeed, 1e-6, "walk speed")
	check(s.facing == 1, "should face right")
	run(s, world, IDLE, math.ceil(0.5 / DT))
	check(s.vx == 0, "should stop with no input")
end)

test("walking into a wall stops at its face", function()
	local world = flatWorld()
	world:addBox(5, 0, 7, 10, "Wall")
	local s = standingAt(world, 0, 0)
	run(s, world, input(1), math.ceil(2 / DT))
	near(s.x, 5 - Config.BodyWidth / 2, 0.001, "x against wall")
	check(s.vx == 0, "speed into wall should be 0")
end)

test("walking off a ledge starts a fall and keeps part of the speed", function()
	local world = CollisionWorld.new()
	world:addBox(-10, -1, 0, 0, "Ground")
	world:addBox(-50, -40, 50, -30, "Ground")
	local s = standingAt(world, -3, 0)
	local events = {}
	runUntil(s, world, input(1), 2, function(st)
		return st.mode == "Air"
	end, events)
	check(hasEvent(events, "WalkOffEdge"), "expected WalkOffEdge")
	near(s.vx, Config.WalkSpeed * Config.WalkOffEdgeSpeedMultiplier, 1e-6, "edge speed")
end)

print("Charged jump")

local function jumpPeak(chargeSteps: number): number
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	-- Hold jump for chargeSteps (or until an auto-jump happens), then release.
	for _ = 1, chargeSteps do
		Sim.step(s, input(0, true), DT, world, Config)
		if s.mode == "Air" then
			break
		end
	end
	local peak = 0
	runUntil(s, world, IDLE, 3, function(st)
		peak = math.max(peak, st.y)
		return st.mode == "Ground"
	end)
	return peak
end

test("a tap gives the minimum jump height", function()
	local expected = Config.MinJumpVelocityY ^ 2 / (2 * Config.Gravity)
	near(jumpPeak(1), expected, 0.25, "tap height")
end)

test("a full charge gives the maximum jump height (auto-jump at full charge)", function()
	local expected = Config.MaxJumpVelocityY ^ 2 / (2 * Config.Gravity)
	-- Hold far longer than MaxChargeTime: AutoJumpAtMaxCharge must launch by itself.
	near(jumpPeak(math.ceil(2 / DT)), expected, 0.3, "full-charge height")
end)

test("longer charge always means a higher jump", function()
	local previous = -1
	for steps = 1, math.ceil(Config.MaxChargeTime / DT), 6 do
		local peak = jumpPeak(steps)
		check(peak > previous, ("charge %d steps gave %.3f, not above %.3f"):format(steps, peak, previous))
		previous = peak
	end
end)

test("jump direction is committed: no air control by default", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	chargedJump(s, world, 30, 1)
	local vxAtLaunch = s.vx
	check(vxAtLaunch > 0, "should jump right")
	run(s, world, input(-1), 10) -- pushing left in the air
	near(s.vx, vxAtLaunch, 1e-9, "air vx must not change")
end)

test("landing stops horizontal motion", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	chargedJump(s, world, 30, 1)
	runUntil(s, world, IDLE, 3, function(st)
		return st.mode == "Ground"
	end)
	check(s.vx == 0, "vx after landing")
	check(s.jumps == 1, "jump counter")
end)

test("charge can be cancelled", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	run(s, world, input(0, true), 20)
	check(s.mode == "Charging", "should be charging")
	local events = run(s, world, input(0, true, true), 1)
	check(hasEvent(events, "ChargeCancel"), "expected ChargeCancel")
	check(s.mode == "Ground", "should be back on ground")
	-- Releasing afterwards must not jump.
	run(s, world, IDLE, 5)
	check(s.jumps == 0, "no jump should have happened")
end)

test("jump held through a landing does not start a new charge", function()
	local world = flatWorld()
	local s = standingAt(world, 0, 0)
	run(s, world, input(0, true), math.ceil(2 / DT)) -- auto-jumps, keeps holding
	runUntil(s, world, input(0, true), 3, function(st)
		return st.mode == "Ground"
	end)
	run(s, world, input(0, true), 10)
	check(s.mode == "Ground", "must not charge until jump is released")
	run(s, world, IDLE, 1)
	run(s, world, input(0, true), 1)
	check(s.mode == "Charging", "a fresh press should charge")
end)

print("Collisions")

test("hitting a ceiling kills upward speed", function()
	local world = flatWorld()
	world:addBox(-10, 5, 10, 6, "Wall") -- ceiling 5 studs above the floor
	local s = standingAt(world, 0, 0)
	local events = chargedJump(s, world, math.ceil(2 / DT), 0)
	local peak = 0
	runUntil(s, world, IDLE, 3, function(st)
		peak = math.max(peak, st.y)
		return st.mode == "Ground"
	end, events)
	check(hasEvent(events, "BumpCeiling"), "expected BumpCeiling")
	check(peak + Config.BodyHeight <= 5 + 1e-3, ("head went through the ceiling (feet peak %.3f)"):format(peak))
end)

test("hitting a wall in the air rebounds by WallBounceFactor", function()
	local world = flatWorld()
	world:addBox(3, 0, 5, 40, "Wall")
	local s = standingAt(world, 0, 0)
	local events = chargedJump(s, world, 40, 1)
	local launchVx = s.vx
	runUntil(s, world, IDLE, 2, function()
		return hasEvent(events, "BumpWall")
	end, events)
	check(hasEvent(events, "BumpWall"), "expected BumpWall")
	near(s.vx, -launchVx * Config.WallBounceFactor, 1e-6, "rebound speed")
	check(s.x + Config.BodyWidth / 2 <= 3 + 1e-3, "body must stay outside the wall")
end)

test("a fast fall never tunnels through a thin platform", function()
	local world = CollisionWorld.new()
	world:addBox(-5, 0, 5, 0.5, "Platform")
	world:addBox(-50, -60, 50, -50, "Ground")
	local s = Sim.newState(0, 200)
	runUntil(s, world, IDLE, 10, function(st)
		return st.mode ~= "Air"
	end)
	near(s.y, 0.5, 0.001, "should land on the thin platform")
end)

test("falling onto a platform's corner lands on it instead of bouncing off", function()
	local world = CollisionWorld.new()
	world:addBox(0, -10, 10, 0, "Ground")
	world:addBox(-100, -60, 100, -50, "Ground")
	-- Body starts left of the platform, slightly above it, drifting right while falling.
	local s = Sim.newState(-1.05, 0.1)
	s.vx = 5
	s.vy = -20
	runUntil(s, world, IDLE, 3, function(st)
		return st.mode ~= "Air"
	end)
	near(s.y, 0, 0.001, "should be standing on the platform")
end)

test("a long fall makes the character splat, then recover", function()
	local world = flatWorld()
	local s = Sim.newState(0, Config.LongFallStunDistance + 5)
	local events = {}
	runUntil(s, world, IDLE, 5, function(st)
		return st.mode ~= "Air"
	end, events)
	check(hasEvent(events, "Splat"), "expected Splat")
	check(s.mode == "Stunned", "should be stunned")
	check(s.falls == 1, "fall counter")
	run(s, world, input(1, true), math.floor(Config.LongFallStunTime / DT) - 2)
	check(s.mode == "Stunned" and s.x == 0, "no movement while stunned")
	run(s, world, input(1, true), 4)
	check(s.mode == "Ground", "should recover")
end)

test("simulation is deterministic", function()
	local function play()
		local world = LevelGeometry.buildWorld(TestLevel)
		local s = Sim.newState(TestLevel.spawn.x, TestLevel.spawn.y)
		local script = { { 1, false, 30 }, { 1, true, 40 }, { 1, false, 200 }, { -1, true, 90 }, { 0, false, 300 } }
		for _, part in script do
			run(s, world, input(part[1], part[2]), part[3])
		end
		return s
	end
	local a, b = play(), play()
	check(a.x == b.x and a.y == b.y and a.vx == b.vx and a.vy == b.vy, "two identical runs diverged")
end)

print("Test level")

-- Tries jumps from many positions, directions and charge lengths on `from` and reports whether
-- any of them lands on `to`.
local function findJump(world, from, to): string?
	local halfW = Config.BodyWidth / 2
	local maxChargeSteps = math.ceil(Config.MaxChargeTime / DT)
	local x = from.minX - halfW + 0.1
	while x < from.maxX + halfW - 0.1 do
		if not world:isBlocked(x - halfW, from.maxY, x + halfW, from.maxY + Config.BodyHeight) then
			for _, dir in { -1, 0, 1 } do
				for chargeSteps = 1, maxChargeSteps, 2 do
					local s = Sim.newState(x, from.maxY)
					run(s, world, IDLE, 1)
					local support = Sim.findSupport(s, world, Config)
					if s.mode ~= "Ground" or not support or support.id ~= from.id then
						break
					end
					chargedJump(s, world, chargeSteps, dir)
					local landed = runUntil(s, world, input(dir), 5, function(st)
						return st.mode ~= "Air"
					end)
					local on = landed and Sim.findSupport(s, world, Config)
					if on and on.id == to.id then
						return ("x=%.1f dir=%d charge=%.2fs"):format(x, dir, chargeSteps * DT)
					end
				end
			end
		end
		x += 0.5
	end
	return nil
end

do
	local world, boxes = LevelGeometry.buildWorld(TestLevel)
	local route = TestLevel.route or {}
	for i = 1, #route - 1 do
		local fromName, toName = route[i], route[i + 1]
		test(("route: %s -> %s is reachable"):format(fromName, toName), function()
			local solution = findJump(world, boxes[fromName], boxes[toName])
			check(solution ~= nil, "no jump found")
			print("        e.g. " .. solution)
		end)
	end

	test("spawn point is on solid ground", function()
		local s = Sim.newState(TestLevel.spawn.x, TestLevel.spawn.y)
		run(s, world, IDLE, 2)
		check(s.mode == "Ground", "spawn should be on ground")
	end)
end

print(("\n%d passed, %d failed"):format(passed, failed))
if failed > 0 then
	process.exit(1)
end
