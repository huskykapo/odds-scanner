--!strict
--[[
	SkyboundClient — client entry point (Rojo turns this folder into a LocalScript in
	StarterPlayerScripts; the other files here become its child ModuleScripts).

	Every rendered frame, in this order:
	  1. read input
	  2. run the fixed-step movement simulation
	  3. react to movement events (sounds, squash, messages)
	  4. draw our character, other players, camera and HUD
	  5. a few times per second, report our state to the server
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")
local StarterGui = game:GetService("StarterGui")
local UserInputService = game:GetService("UserInputService")
local Workspace = game:GetService("Workspace")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local MovementSimulator = require(Shared.Movement.MovementSimulator)
local LevelGeometry = require(Shared.Levels.LevelGeometry)
local Net = require(Shared.Net)

local InputController = require(script.InputController)
local MovementController = require(script.MovementController)
local CameraController = require(script.CameraController)
local CharacterRenderer = require(script.CharacterRenderer)
local RemotePlayers = require(script.RemotePlayers)
local AudioController = require(script.AudioController)
local DebugHUD = require(script.DebugHUD)

local camera = Workspace.CurrentCamera

-- ── Level ──
local levelModule = Shared.Levels:WaitForChild(GameConfig.ActiveLevel)
local level = require(levelModule) :: any
local world = LevelGeometry.buildWorld(level)

-- ── Controllers ──
local input = InputController.new()
local movement = MovementController.new(world, level.spawn.x, level.spawn.y)
local character = CharacterRenderer.new({ color = GameConfig.PlaceholderColor })
local cameraController = CameraController.new(camera, level)
local remotePlayers = RemotePlayers.new()
local audio = AudioController.new()
local hud = DebugHUD.new(level.spawn.y)

local stateRemote = Net.get("StateUpdate")
local requestRespawnRemote = Net.get("RequestRespawn")

cameraController:snapTo(level.spawn.x, level.spawn.y)

local summitAnnounced = false

-- ── Respawn (server decides where) ──
local function requestRespawn()
	requestRespawnRemote:FireServer()
end

Net.get("Respawn").OnClientEvent:Connect(function(x: number, y: number)
	if typeof(x) ~= "number" or typeof(y) ~= "number" then
		return
	end
	movement:teleport(x, y)
	cameraController:snapTo(x, y)
	summitAnnounced = false
	hud:showMessage("Back to the start", 1.5)
end)

if GameConfig.Debug.AllowResetKey then
	UserInputService.InputBegan:Connect(function(inputObject, processed)
		if not processed and inputObject.KeyCode == Enum.KeyCode.R then
			requestRespawn()
		end
	end)
end

-- The Roblox menu's "Reset Character" button has no character to reset; make it restart instead.
local resetEvent = Instance.new("BindableEvent")
resetEvent.Event:Connect(requestRespawn)
task.spawn(function()
	-- SetCore fails until the Roblox core scripts are ready, so retry for a few seconds.
	for _ = 1, 20 do
		if pcall(StarterGui.SetCore, StarterGui, "ResetButtonCallback", resetEvent) then
			return
		end
		task.wait(0.5)
	end
end)

-- ── Main loop ──
local sendInterval = 1 / GameConfig.StateSendRate
local sendTimer = 0

local function onFrame(dt: number)
	local state = movement.state
	local events = movement:update(dt, input:read())

	for _, event in events do
		character:playEvent(event.kind, event.value)
		audio:playEvent(event.kind, event.value)
		if event.kind == "Splat" then
			hud:showMessage(("Ouch! Fell %d studs"):format(math.round(event.value)), 2)
		elseif event.kind == "Land" and state.groundKind == "Goal" and not summitAnnounced then
			summitAnnounced = true
			hud:showMessage("Summit reached!", 4)
		end
	end

	local x, y = movement:getRenderPosition()
	character:render(dt, x, y, state.mode, state.facing, state.vx, state.vy, MovementSimulator.getChargeRatio(state))
	remotePlayers:update(dt)
	cameraController:update(dt, x, y)
	hud:update(dt, state)

	sendTimer += dt
	if sendTimer >= sendInterval then
		sendTimer = 0
		stateRemote:FireServer(
			Net.encodeState(state.x, state.y, state.vx, state.vy, state.mode, state.facing, MovementSimulator.getChargeRatio(state))
		)
	end
end

-- Runs right after Roblox's own camera step, so nothing overrides our camera afterwards.
RunService:BindToRenderStep("SkyboundFrame", Enum.RenderPriority.Camera.Value + 1, onFrame)

print(("[Skybound] Client ready: %s"):format(level.displayName))
