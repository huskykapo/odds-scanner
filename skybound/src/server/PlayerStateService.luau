--!strict
--[[
	PlayerStateService — server side of movement.

	The client simulates its own movement (so controls feel instant) and reports its state
	about 20 times per second. The server:
	  * checks every report for plausibility (speed limit, not inside solid geometry),
	  * remembers each player's last trusted position and highest altitude,
	  * broadcasts everyone's position to everyone (so players see each other),
	  * handles respawn requests (the server decides where you respawn).

	Current integrity level: suspicious reports are IGNORED for scoring (highest altitude) and
	logged, but the player is not corrected or kicked yet. Later phases add correction.
]]

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local MovementConfig = require(Shared.Config.MovementConfig)
local Net = require(Shared.Net)
local LevelGeometry = require(Shared.Levels.LevelGeometry)
local CollisionWorld = require(Shared.Movement.CollisionWorld)

type LevelData = LevelGeometry.LevelData

type PlayerRecord = {
	x: number,
	y: number,
	mode: string,
	facing: number,
	charge: number,
	lastReportTime: number,
	highestY: number,
	strikes: number,
	lastRespawnRequest: number,
	focus: Part,
}

local PlayerStateService = {}

local records: { [Player]: PlayerRecord } = {}
local level: LevelData
local world: CollisionWorld.CollisionWorld

-- Fastest legitimate speed (studs/s) the movement model can produce.
local function maxLegitSpeed(): number
	local cfg = MovementConfig
	local maxVx = math.max(cfg.WalkSpeed, cfg.MaxJumpVelocityX, cfg.MinJumpVelocityX, cfg.AirControlMaxSpeed)
	local maxVy = math.max(cfg.MaxJumpVelocityY, cfg.MaxFallSpeed)
	return math.sqrt(maxVx * maxVx + maxVy * maxVy)
end

local function insideSolid(x: number, y: number): boolean
	-- Shrink the body slightly so legitimate contact (and float rounding) never counts.
	local halfW = MovementConfig.BodyWidth / 2 - 0.1
	return world:isBlocked(x - halfW, y + 0.1, x + halfW, y + MovementConfig.BodyHeight - 0.1)
end

local function worldPosition(x: number, y: number): Vector3
	return GameConfig.WorldOrigin + Vector3.new(x, y + MovementConfig.BodyHeight / 2, 0)
end

local function onPlayerAdded(player: Player)
	-- An invisible anchor so StreamingEnabled streams the area around the player's 2D position
	-- (there is no Roblox character to stream around).
	local focus = Instance.new("Part")
	focus.Name = "SkyboundFocus_" .. player.UserId
	focus.Anchored = true
	focus.CanCollide = false
	focus.CanQuery = false
	focus.CanTouch = false
	focus.Transparency = 1
	focus.Size = Vector3.one
	focus.CFrame = CFrame.new(worldPosition(level.spawn.x, level.spawn.y))
	focus.Parent = Workspace

	records[player] = {
		x = level.spawn.x,
		y = level.spawn.y,
		mode = "Air",
		facing = 1,
		charge = 0,
		lastReportTime = os.clock(),
		highestY = level.spawn.y,
		strikes = 0,
		lastRespawnRequest = 0,
		focus = focus,
	}
	player.ReplicationFocus = focus
end

local function onPlayerRemoving(player: Player)
	local record = records[player]
	if record then
		record.focus:Destroy()
		records[player] = nil
	end
end

local function onStateUpdate(player: Player, payload: any)
	local record = records[player]
	if not record then
		return
	end
	local reported = Net.decodeState(payload)
	if not reported then
		return
	end

	local now = os.clock()
	local elapsed = now - record.lastReportTime
	local validation = GameConfig.Validation
	local allowed = maxLegitSpeed() * validation.SpeedTolerance * (elapsed + validation.JitterAllowance)
	local dx, dy = reported.x - record.x, reported.y - record.y
	local distance = math.sqrt(dx * dx + dy * dy)

	local suspicious = distance > allowed or insideSolid(reported.x, reported.y)
	if suspicious then
		record.strikes += 1
		if record.strikes == validation.StrikesBeforeWarning then
			warn(
				("[Skybound] Suspicious movement from %s: moved %.1f studs in %.2fs (allowed %.1f) to (%.1f, %.1f)"):format(
					player.Name,
					distance,
					elapsed,
					allowed,
					reported.x,
					reported.y
				)
			)
		end
	else
		record.strikes = 0
		record.highestY = math.max(record.highestY, reported.y)
	end

	-- For now, positions are accepted for display either way (see the header comment).
	record.x = reported.x
	record.y = reported.y
	record.mode = reported.mode
	record.facing = reported.facing
	record.charge = reported.charge
	record.lastReportTime = now
	record.focus.CFrame = CFrame.new(worldPosition(reported.x, reported.y))
end

local function onRespawnRequest(player: Player)
	local record = records[player]
	if not record then
		return
	end
	local now = os.clock()
	if now - record.lastRespawnRequest < 0.5 then
		return -- rate limit
	end
	record.lastRespawnRequest = now
	record.x = level.spawn.x
	record.y = level.spawn.y
	record.lastReportTime = now
	record.strikes = 0
	Net.get("Respawn"):FireClient(player, level.spawn.x, level.spawn.y)
end

local function broadcastLoop()
	local remote = Net.get("PlayerStates")
	local entries: { Net.SnapshotEntry } = {}
	while true do
		task.wait(1 / GameConfig.StateBroadcastRate)
		table.clear(entries)
		for player, record in records do
			table.insert(entries, {
				userId = player.UserId,
				x = record.x,
				y = record.y,
				mode = record.mode,
				facing = record.facing,
				charge = record.charge,
			})
		end
		if #entries > 1 then -- nobody else to show when alone
			for first = 1, #entries, Net.MaxEntriesPerSnapshot do
				local last = math.min(first + Net.MaxEntriesPerSnapshot - 1, #entries)
				remote:FireAllClients(Net.encodeSnapshot(entries, first, last))
			end
		end
	end
end

-- Returns the highest trusted altitude a player reached (for future scoring).
function PlayerStateService.getHighestY(player: Player): number?
	local record = records[player]
	return record and record.highestY
end

function PlayerStateService.start(levelData: LevelData)
	level = levelData
	world = LevelGeometry.buildWorld(levelData)

	Net.get("StateUpdate").OnServerEvent:Connect(onStateUpdate)
	Net.get("RequestRespawn").OnServerEvent:Connect(onRespawnRequest)
	Players.PlayerAdded:Connect(onPlayerAdded)
	Players.PlayerRemoving:Connect(onPlayerRemoving)
	for _, player in Players:GetPlayers() do
		onPlayerAdded(player)
	end
	task.spawn(broadcastLoop)
end

return PlayerStateService
