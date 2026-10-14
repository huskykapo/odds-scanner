--!strict
--[[
	MovementController — runs the local player's MovementSimulator at a fixed time step.

	Frames arrive at uneven intervals (30, 60, 144 FPS...). We bank the elapsed time and run as
	many fixed simulation steps as fit ("fixed timestep with accumulator"). The drawn position is
	interpolated between the last two steps so motion stays smooth on any refresh rate.
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local MovementConfig = require(Shared.Config.MovementConfig)
local MovementSimulator = require(Shared.Movement.MovementSimulator)
local CollisionWorld = require(Shared.Movement.CollisionWorld)

local STEP = 1 / MovementConfig.SimulationHz

local MovementController = {}
MovementController.__index = MovementController

export type MovementController = typeof(setmetatable(
	{} :: {
		world: CollisionWorld.CollisionWorld,
		state: MovementSimulator.State,
		prevX: number,
		prevY: number,
		accumulator: number,
		events: { MovementSimulator.Event },
	},
	MovementController
))

function MovementController.new(world: CollisionWorld.CollisionWorld, x: number, y: number): MovementController
	return setmetatable({
		world = world,
		state = MovementSimulator.newState(x, y),
		prevX = x,
		prevY = y,
		accumulator = 0,
		events = {},
	}, MovementController)
end

-- Advances by one rendered frame. Returns the events that happened (reused table: read it now).
function MovementController.update(self: MovementController, frameDt: number, input: MovementSimulator.Input): { MovementSimulator.Event }
	table.clear(self.events)
	-- Ignore huge gaps (e.g. the window was dragged) instead of fast-forwarding through them.
	self.accumulator += math.min(frameDt, 0.25)

	local steps = 0
	while self.accumulator >= STEP and steps < MovementConfig.MaxStepsPerFrame do
		self.prevX, self.prevY = self.state.x, self.state.y
		MovementSimulator.step(self.state, input, STEP, self.world, MovementConfig, self.events)
		self.accumulator -= STEP
		steps += 1
	end
	if steps == MovementConfig.MaxStepsPerFrame then
		self.accumulator = 0 -- drop the backlog rather than spiral
	end
	return self.events
end

-- Smoothed position for drawing (between the previous and current simulation step).
function MovementController.getRenderPosition(self: MovementController): (number, number)
	local alpha = math.clamp(self.accumulator / STEP, 0, 1)
	local s = self.state
	return self.prevX + (s.x - self.prevX) * alpha, self.prevY + (s.y - self.prevY) * alpha
end

function MovementController.teleport(self: MovementController, x: number, y: number)
	MovementSimulator.teleport(self.state, x, y)
	self.prevX, self.prevY = x, y
	self.accumulator = 0
end

return MovementController
