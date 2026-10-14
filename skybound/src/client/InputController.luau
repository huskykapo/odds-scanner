--!strict
--[[
	InputController — turns keyboard, gamepad and touch into one simple input:
	  moveX (-1, 0, 1), jumpHeld, cancelHeld.

	Keyboard: A/D or Left/Right move · Space, W or Up charge & jump · S or Down cancel a charge
	Gamepad:  left stick / D-pad move · A charge & jump · B cancel
	Touch:    on-screen ◀ ▶ buttons and a JUMP button (only on touch devices)
]]

local GuiService = game:GetService("GuiService")
local Players = game:GetService("Players")
local UserInputService = game:GetService("UserInputService")

local LEFT_KEYS = { Enum.KeyCode.A, Enum.KeyCode.Left }
local RIGHT_KEYS = { Enum.KeyCode.D, Enum.KeyCode.Right }
local JUMP_KEYS = { Enum.KeyCode.Space, Enum.KeyCode.W, Enum.KeyCode.Up }
local CANCEL_KEYS = { Enum.KeyCode.S, Enum.KeyCode.Down }
local STICK_DEADZONE = 0.4

export type Input = { moveX: number, jumpHeld: boolean, cancelHeld: boolean }

local InputController = {}
InputController.__index = InputController

export type InputController = typeof(setmetatable(
	{} :: {
		connections: { RBXScriptConnection },
		gamepad: { left: boolean, right: boolean, jump: boolean, cancel: boolean, stickX: number },
		-- touch button name -> the InputObject currently holding it
		touchHeld: { [string]: InputObject? },
		touchGui: ScreenGui?,
		current: Input,
	},
	InputController
))

local function anyKeyDown(keys: { Enum.KeyCode }): boolean
	for _, key in keys do
		if UserInputService:IsKeyDown(key) then
			return true
		end
	end
	return false
end

function InputController.new(): InputController
	local self = setmetatable({
		connections = {},
		gamepad = { left = false, right = false, jump = false, cancel = false, stickX = 0 },
		touchHeld = {},
		touchGui = nil,
		current = { moveX = 0, jumpHeld = false, cancelHeld = false },
	}, InputController)

	self:_bindGamepad()
	if UserInputService.TouchEnabled then
		self:_createTouchControls()
	end
	return self
end

function InputController._bindGamepad(self: InputController)
	local pad = self.gamepad
	local function setButton(keyCode: Enum.KeyCode, down: boolean)
		if keyCode == Enum.KeyCode.DPadLeft then
			pad.left = down
		elseif keyCode == Enum.KeyCode.DPadRight then
			pad.right = down
		elseif keyCode == Enum.KeyCode.ButtonA then
			pad.jump = down
		elseif keyCode == Enum.KeyCode.ButtonB then
			pad.cancel = down
		end
	end
	local function isGamepad(input: InputObject): boolean
		return input.UserInputType == Enum.UserInputType.Gamepad1
	end

	table.insert(
		self.connections,
		UserInputService.InputBegan:Connect(function(input)
			if isGamepad(input) then
				setButton(input.KeyCode, true)
			end
		end)
	)
	table.insert(
		self.connections,
		UserInputService.InputEnded:Connect(function(input)
			if isGamepad(input) then
				setButton(input.KeyCode, false)
			end
		end)
	)
	table.insert(
		self.connections,
		UserInputService.InputChanged:Connect(function(input)
			if isGamepad(input) and input.KeyCode == Enum.KeyCode.Thumbstick1 then
				pad.stickX = input.Position.X
			end
		end)
	)
	table.insert(
		self.connections,
		UserInputService.GamepadDisconnected:Connect(function()
			pad.left, pad.right, pad.jump, pad.cancel, pad.stickX = false, false, false, false, 0
		end)
	)
end

function InputController._createTouchControls(self: InputController)
	-- Hide Roblox's default thumbstick/jump button; we draw our own.
	pcall(function()
		GuiService.TouchControlsEnabled = false
	end)

	local gui = Instance.new("ScreenGui")
	gui.Name = "SkyboundTouchControls"
	gui.ResetOnSpawn = false
	gui.IgnoreGuiInset = true
	gui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling

	local function makeButton(name: string, text: string, anchor: Vector2, position: UDim2, size: number)
		local button = Instance.new("TextButton")
		button.Name = name
		button.Text = text
		button.Font = Enum.Font.Arcade
		button.TextScaled = true
		button.TextColor3 = Color3.new(1, 1, 1)
		button.BackgroundColor3 = Color3.fromRGB(30, 30, 45)
		button.BackgroundTransparency = 0.35
		button.AutoButtonColor = false
		button.AnchorPoint = anchor
		button.Position = position
		button.Size = UDim2.fromOffset(size, size)
		local corner = Instance.new("UICorner")
		corner.CornerRadius = UDim.new(0, 12)
		corner.Parent = button
		local stroke = Instance.new("UIStroke")
		stroke.Thickness = 3
		stroke.Color = Color3.fromRGB(255, 255, 255)
		stroke.Transparency = 0.4
		stroke.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
		stroke.Parent = button
		local padding = Instance.new("UIPadding")
		padding.PaddingTop = UDim.new(0.2, 0)
		padding.PaddingBottom = UDim.new(0.2, 0)
		padding.Parent = button

		-- A button stays held until the SAME finger is lifted, even if it slides off the button.
		table.insert(
			self.connections,
			button.InputBegan:Connect(function(input)
				if input.UserInputType == Enum.UserInputType.Touch then
					self.touchHeld[name] = input
					button.BackgroundTransparency = 0.1
				end
			end)
		)
		button.Parent = gui
		return button
	end

	local buttons: { [string]: TextButton } = {
		Left = makeButton("Left", "<", Vector2.new(0, 1), UDim2.new(0, 24, 1, -24), 96),
		Right = makeButton("Right", ">", Vector2.new(0, 1), UDim2.new(0, 132, 1, -24), 96),
		Jump = makeButton("Jump", "JUMP", Vector2.new(1, 1), UDim2.new(1, -24, 1, -24), 120),
	}

	table.insert(
		self.connections,
		UserInputService.TouchEnded:Connect(function(touch)
			for name, held in self.touchHeld do
				if held == touch then
					self.touchHeld[name] = nil
					buttons[name].BackgroundTransparency = 0.35
				end
			end
		end)
	)

	gui.Parent = Players.LocalPlayer:WaitForChild("PlayerGui")
	self.touchGui = gui
end

-- Samples all devices and returns the combined input (the same table every call).
function InputController.read(self: InputController): Input
	local typing = UserInputService:GetFocusedTextBox() ~= nil
	local pad = self.gamepad

	local left = pad.left or pad.stickX < -STICK_DEADZONE or self.touchHeld.Left ~= nil
	local right = pad.right or pad.stickX > STICK_DEADZONE or self.touchHeld.Right ~= nil
	local jump = pad.jump or self.touchHeld.Jump ~= nil
	local cancel = pad.cancel
	if not typing then
		left = left or anyKeyDown(LEFT_KEYS)
		right = right or anyKeyDown(RIGHT_KEYS)
		jump = jump or anyKeyDown(JUMP_KEYS)
		cancel = cancel or anyKeyDown(CANCEL_KEYS)
	end

	local current = self.current
	current.moveX = (if right then 1 else 0) - (if left then 1 else 0)
	current.jumpHeld = jump
	current.cancelHeld = cancel
	return current
end

function InputController.destroy(self: InputController)
	for _, connection in self.connections do
		connection:Disconnect()
	end
	table.clear(self.connections)
	if self.touchGui then
		self.touchGui:Destroy()
		self.touchGui = nil
	end
end

return InputController
