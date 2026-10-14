--!strict
--[[
	DebugHUD — development overlay: altitude, stats, movement state, FPS, a charge meter,
	and short on-screen messages. F1 shows/hides it.

	Not the final game HUD (that comes with the UI phase); it exists to make tuning easy.
]]

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local UserInputService = game:GetService("UserInputService")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local MovementSimulator = require(Shared.Movement.MovementSimulator)

local TEXT_REFRESH = 0.1 -- seconds between text updates (cheaper than every frame)

local DebugHUD = {}
DebugHUD.__index = DebugHUD

export type DebugHUD = typeof(setmetatable(
	{} :: {
		gui: ScreenGui,
		stats: TextLabel,
		message: TextLabel,
		chargeBar: Frame,
		chargeFill: Frame,
		refreshTimer: number,
		messageTimer: number,
		frames: number,
		fpsTimer: number,
		fps: number,
		baseY: number,
		connections: { RBXScriptConnection },
	},
	DebugHUD
))

local function label(name: string, parent: Instance): TextLabel
	local text = Instance.new("TextLabel")
	text.Name = name
	text.BackgroundTransparency = 1
	text.Font = Enum.Font.Code
	text.TextColor3 = Color3.new(1, 1, 1)
	text.TextStrokeTransparency = 0.4
	text.Parent = parent
	return text
end

-- baseY: altitude 0 for display (the level's spawn height).
function DebugHUD.new(baseY: number): DebugHUD
	local gui = Instance.new("ScreenGui")
	gui.Name = "SkyboundDebugHUD"
	gui.ResetOnSpawn = false
	gui.Enabled = GameConfig.Debug.ShowHud

	local stats = label("Stats", gui)
	stats.Position = UDim2.fromOffset(12, 12)
	stats.Size = UDim2.fromOffset(330, 200)
	stats.TextXAlignment = Enum.TextXAlignment.Left
	stats.TextYAlignment = Enum.TextYAlignment.Top
	stats.TextSize = 15

	local message = label("Message", gui)
	message.AnchorPoint = Vector2.new(0.5, 0)
	message.Position = UDim2.new(0.5, 0, 0, 40)
	message.Size = UDim2.new(1, -40, 0, 40)
	message.Font = Enum.Font.Arcade
	message.TextSize = 32
	message.Visible = false

	local chargeBar = Instance.new("Frame")
	chargeBar.Name = "ChargeBar"
	chargeBar.AnchorPoint = Vector2.new(0.5, 1)
	chargeBar.Position = UDim2.new(0.5, 0, 1, -24)
	chargeBar.Size = UDim2.fromOffset(220, 14)
	chargeBar.BackgroundColor3 = Color3.fromRGB(20, 20, 30)
	chargeBar.BackgroundTransparency = 0.3
	chargeBar.BorderSizePixel = 0
	chargeBar.Visible = false
	chargeBar.Parent = gui
	local chargeFill = Instance.new("Frame")
	chargeFill.Name = "Fill"
	chargeFill.Size = UDim2.fromScale(0, 1)
	chargeFill.BackgroundColor3 = Color3.fromRGB(255, 200, 80)
	chargeFill.BorderSizePixel = 0
	chargeFill.Parent = chargeBar

	gui.Parent = Players.LocalPlayer:WaitForChild("PlayerGui")

	local self = setmetatable({
		gui = gui,
		stats = stats,
		message = message,
		chargeBar = chargeBar,
		chargeFill = chargeFill,
		refreshTimer = 0,
		messageTimer = 0,
		frames = 0,
		fpsTimer = 0,
		fps = 0,
		baseY = baseY,
		connections = {},
	}, DebugHUD)

	table.insert(
		self.connections,
		UserInputService.InputBegan:Connect(function(input, processed)
			if not processed and input.KeyCode == Enum.KeyCode.F1 then
				gui.Enabled = not gui.Enabled
			end
		end)
	)
	return self
end

function DebugHUD.showMessage(self: DebugHUD, text: string, seconds: number?)
	self.message.Text = text
	self.message.Visible = true
	self.messageTimer = seconds or 2.5
end

function DebugHUD.update(self: DebugHUD, dt: number, state: MovementSimulator.State)
	self.frames += 1
	self.fpsTimer += dt
	if self.fpsTimer >= 0.5 then
		self.fps = self.frames / self.fpsTimer
		self.frames, self.fpsTimer = 0, 0
	end

	if self.messageTimer > 0 then
		self.messageTimer -= dt
		if self.messageTimer <= 0 then
			self.message.Visible = false
		end
	end

	local charge = MovementSimulator.getChargeRatio(state)
	self.chargeBar.Visible = GameConfig.Debug.ShowChargeMeter and state.mode == "Charging"
	self.chargeFill.Size = UDim2.fromScale(charge, 1)

	self.refreshTimer -= dt
	if self.refreshTimer > 0 or not self.gui.Enabled then
		return
	end
	self.refreshTimer = TEXT_REFRESH

	local keys = if GameConfig.Debug.AllowResetKey then "  R restart" else ""
	self.stats.Text = table.concat({
		("Altitude   %6.1f"):format(state.y - self.baseY),
		("Best       %6.1f"):format(state.highestY - self.baseY),
		("Mode       %s"):format(state.mode),
		("Velocity   %5.1f, %5.1f"):format(state.vx, state.vy),
		("Last jump  %3d%% power"):format(math.round(state.lastJumpPower * 100)),
		("Jumps %d   Falls %d   Last fall %.1f"):format(state.jumps, state.falls, state.lastFallDistance),
		("FPS        %d"):format(math.round(self.fps)),
		"",
		"A/D move  Space charge+jump",
		"S cancel charge  F1 hide" .. keys,
	}, "\n")
end

function DebugHUD.destroy(self: DebugHUD)
	for _, connection in self.connections do
		connection:Disconnect()
	end
	self.gui:Destroy()
end

return DebugHUD
