--!strict
--[[
	RemotePlayers — draws the other players from the server's position snapshots.

	Snapshots arrive ~15 times per second; each character glides smoothly toward its latest
	reported position. Other players are purely visual: they are not part of the collision
	world, so they can never block or push you.
]]

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local Net = require(Shared.Net)
local CharacterRenderer = require(script.Parent.CharacterRenderer)

-- Positions further apart than this are snapped instead of glided (e.g. a respawn).
local SNAP_DISTANCE = 20
local FOLLOW_SHARPNESS = 15

type Entry = {
	renderer: CharacterRenderer.CharacterRenderer,
	x: number,
	y: number,
	targetX: number,
	targetY: number,
	vx: number,
	vy: number,
	mode: string,
	facing: number,
	charge: number,
}

local RemotePlayers = {}
RemotePlayers.__index = RemotePlayers

export type RemotePlayers = typeof(setmetatable(
	{} :: {
		entries: { [number]: Entry },
		connections: { RBXScriptConnection },
	},
	RemotePlayers
))

local function colorFor(userId: number): Color3
	-- A stable, pleasant colour per player.
	return Color3.fromHSV((userId * 0.618034) % 1, 0.55, 0.95)
end

function RemotePlayers.new(): RemotePlayers
	local self = setmetatable({ entries = {}, connections = {} }, RemotePlayers)
	table.insert(
		self.connections,
		Net.get("PlayerStates").OnClientEvent:Connect(function(payload)
			self:_applySnapshot(payload)
		end)
	)
	table.insert(
		self.connections,
		Players.PlayerRemoving:Connect(function(player)
			self:_remove(player.UserId)
		end)
	)
	return self
end

function RemotePlayers._applySnapshot(self: RemotePlayers, payload: any)
	local localUserId = Players.LocalPlayer.UserId
	for _, data in Net.decodeSnapshot(payload) do
		if data.userId ~= localUserId then
			local entry = self.entries[data.userId]
			if not entry then
				local player = Players:GetPlayerByUserId(data.userId)
				if not player then
					continue -- already left
				end
				entry = {
					renderer = CharacterRenderer.new({
						color = colorFor(data.userId),
						name = player.DisplayName,
						zOffset = -0.1 - (data.userId % 7) * 0.01,
					}),
					x = data.x,
					y = data.y,
					targetX = data.x,
					targetY = data.y,
					vx = 0,
					vy = 0,
					mode = data.mode,
					facing = data.facing,
					charge = data.charge,
				}
				self.entries[data.userId] = entry
			end
			entry.targetX = data.x
			entry.targetY = data.y
			entry.mode = data.mode
			entry.facing = data.facing
			entry.charge = data.charge
		end
	end
end

function RemotePlayers._remove(self: RemotePlayers, userId: number)
	local entry = self.entries[userId]
	if entry then
		entry.renderer:destroy()
		self.entries[userId] = nil
	end
end

function RemotePlayers.update(self: RemotePlayers, dt: number)
	if dt <= 0 then
		return
	end
	local blend = 1 - math.exp(-FOLLOW_SHARPNESS * dt)
	for _, entry in self.entries do
		local dx, dy = entry.targetX - entry.x, entry.targetY - entry.y
		local oldX, oldY = entry.x, entry.y
		if dx * dx + dy * dy > SNAP_DISTANCE * SNAP_DISTANCE then
			entry.x, entry.y = entry.targetX, entry.targetY
		else
			entry.x += dx * blend
			entry.y += dy * blend
		end
		entry.vx = (entry.x - oldX) / dt
		entry.vy = (entry.y - oldY) / dt
		entry.renderer:render(dt, entry.x, entry.y, entry.mode, entry.facing, entry.vx, entry.vy, entry.charge)
	end
end

function RemotePlayers.destroy(self: RemotePlayers)
	for _, connection in self.connections do
		connection:Disconnect()
	end
	for userId in self.entries do
		self:_remove(userId)
	end
end

return RemotePlayers
