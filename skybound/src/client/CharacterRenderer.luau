--!strict
--[[
	CharacterRenderer — draws one character (local or remote) from its movement state.

	PLACEHOLDER: a coloured block with an "eye" showing the facing direction, squashed and
	stretched to show each movement state. Phase 3 replaces the inside of this module with an
	animated pixel-art sprite; the interface (render / playEvent / getAnimState) stays the same,
	so the rest of the game does not change.

	The visuals never affect gameplay: collision uses the fixed body size from MovementConfig.
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local MovementConfig = require(Shared.Config.MovementConfig)

export type AnimState = "Idle" | "Walk" | "Charge" | "Rise" | "Fall" | "Land" | "Splat"

local LAND_POSE_TIME = 0.12
local EYE_COLOR = Color3.fromRGB(25, 20, 35)

local CharacterRenderer = {}
CharacterRenderer.__index = CharacterRenderer

export type Options = {
	color: Color3,
	name: string?, -- shown above the head (used for other players)
	zOffset: number?, -- small depth offset so overlapping characters do not flicker
}

export type CharacterRenderer = typeof(setmetatable(
	{} :: {
		model: Model,
		body: Part,
		eye: Part,
		zOffset: number,
		scaleX: number,
		scaleY: number,
		landTimer: number,
		bumpTimer: number,
		animState: AnimState,
	},
	CharacterRenderer
))

local function makeVisualPart(name: string, color: Color3, parent: Instance): Part
	local part = Instance.new("Part")
	part.Name = name
	part.Anchored = true
	part.CanCollide = false
	part.CanQuery = false
	part.CanTouch = false
	part.CastShadow = false
	part.Material = Enum.Material.SmoothPlastic
	part.Color = color
	part.Parent = parent
	return part
end

local function getContainer(): Instance
	local container = Workspace:FindFirstChild("SkyboundCharacters")
	if not container then
		container = Instance.new("Folder")
		container.Name = "SkyboundCharacters"
		container.Parent = Workspace
	end
	return container
end

function CharacterRenderer.new(options: Options): CharacterRenderer
	local model = Instance.new("Model")
	model.Name = options.name or "LocalCharacter"
	local body = makeVisualPart("Body", options.color, model)
	local eye = makeVisualPart("Eye", EYE_COLOR, model)
	model.PrimaryPart = body

	if options.name then
		local billboard = Instance.new("BillboardGui")
		billboard.Name = "NameTag"
		billboard.Adornee = body
		billboard.Size = UDim2.fromOffset(160, 24)
		billboard.StudsOffsetWorldSpace = Vector3.new(0, MovementConfig.BodyHeight / 2 + 1.2, 0)
		billboard.AlwaysOnTop = true
		billboard.LightInfluence = 0
		local label = Instance.new("TextLabel")
		label.Size = UDim2.fromScale(1, 1)
		label.BackgroundTransparency = 1
		label.Font = Enum.Font.Arcade
		label.TextScaled = true
		label.TextColor3 = Color3.new(1, 1, 1)
		label.TextStrokeTransparency = 0.3
		label.Text = options.name
		label.Parent = billboard
		billboard.Parent = model
	end

	model.Parent = getContainer()

	return setmetatable({
		model = model,
		body = body,
		eye = eye,
		zOffset = options.zOffset or 0,
		scaleX = 1,
		scaleY = 1,
		landTimer = 0,
		bumpTimer = 0,
		animState = "Idle" :: AnimState,
	}, CharacterRenderer)
end

-- Reacts to a movement event (see MovementSimulator.EventKind).
function CharacterRenderer.playEvent(self: CharacterRenderer, kind: string, _value: number)
	if kind == "Land" then
		self.landTimer = LAND_POSE_TIME
	elseif kind == "BumpWall" or kind == "BumpCeiling" then
		self.bumpTimer = 0.1
	end
end

-- Which animation a sprite renderer should play for this state.
function CharacterRenderer.getAnimState(self: CharacterRenderer, mode: string, vx: number, vy: number): AnimState
	if mode == "Stunned" then
		return "Splat"
	elseif mode == "Charging" then
		return "Charge"
	elseif mode == "Air" then
		return if vy > 0 then "Rise" else "Fall"
	elseif self.landTimer > 0 then
		return "Land"
	elseif math.abs(vx) > 0.1 then
		return "Walk"
	end
	return "Idle"
end

--[[
	Draws the character with its feet at gameplay position (x, y).
	vx/vy only choose the pose; charge is 0..1 while charging.
]]
function CharacterRenderer.render(
	self: CharacterRenderer,
	dt: number,
	x: number,
	y: number,
	mode: string,
	facing: number,
	vx: number,
	vy: number,
	charge: number
)
	self.landTimer = math.max(0, self.landTimer - dt)
	self.bumpTimer = math.max(0, self.bumpTimer - dt)
	local anim = self:getAnimState(mode, vx, vy)
	if anim ~= self.animState then
		self.animState = anim
		self.model:SetAttribute("AnimState", anim)
	end

	-- Target squash/stretch for the pose.
	local targetX, targetY = 1, 1
	if anim == "Charge" then
		targetY = 1 - 0.3 * charge
		targetX = 1 + 0.15 * charge
	elseif anim == "Rise" then
		targetX, targetY = 0.88, 1.12
	elseif anim == "Fall" then
		targetX, targetY = 0.94, 1.06
	elseif anim == "Land" then
		targetX, targetY = 1.2, 0.8
	elseif anim == "Splat" then
		targetX, targetY = 1.45, 0.45
	end
	if self.bumpTimer > 0 then
		targetX *= 0.9
	end
	local blend = 1 - math.exp(-30 * dt)
	self.scaleX += (targetX - self.scaleX) * blend
	self.scaleY += (targetY - self.scaleY) * blend

	local width = MovementConfig.BodyWidth * self.scaleX
	local height = MovementConfig.BodyHeight * self.scaleY
	local depth = 0.5
	local z = GameConfig.BlockDepth / 2 + 0.3 + self.zOffset
	local base = GameConfig.WorldOrigin + Vector3.new(x, y, z)

	self.body.Size = Vector3.new(width, height, depth)
	self.body.CFrame = CFrame.new(base + Vector3.new(0, height / 2, 0))

	local eyeSize = Vector3.new(0.45 * self.scaleX, 0.6 * self.scaleY, 0.05)
	self.eye.Size = eyeSize
	self.eye.CFrame = CFrame.new(base + Vector3.new(facing * width * 0.22, height * 0.7, depth / 2 + 0.03))
end

function CharacterRenderer.destroy(self: CharacterRenderer)
	self.model:Destroy()
end

return CharacterRenderer
