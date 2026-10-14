--!strict
--[[
	LevelBuilder — creates the visible Parts for a level from its data.

	These Parts are VISUALS ONLY: collision comes from the same level data through
	LevelGeometry/CollisionWorld. So the parts have collisions, queries and touches turned off,
	which also keeps them cheap for the physics engine.
]]

local Workspace = game:GetService("Workspace")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local LevelGeometry = require(Shared.Levels.LevelGeometry)

type LevelData = LevelGeometry.LevelData

-- Placeholder colours per block kind (to be replaced by pixel-art textures).
local KIND_COLORS: { [string]: Color3 } = {
	Ground = Color3.fromRGB(92, 72, 60),
	Platform = Color3.fromRGB(150, 140, 175),
	Wall = Color3.fromRGB(105, 98, 125),
	Goal = Color3.fromRGB(245, 190, 60),
}
local GRASS_COLOR = Color3.fromRGB(110, 190, 90)
local SKY_COLOR = Color3.fromRGB(125, 180, 230)

local LevelBuilder = {}

local function makePart(name: string, size: Vector3, cframe: CFrame, color: Color3, parent: Instance): Part
	local part = Instance.new("Part")
	part.Name = name
	part.Anchored = true
	part.CanCollide = false
	part.CanQuery = false
	part.CanTouch = false
	part.CastShadow = false
	part.Material = Enum.Material.SmoothPlastic
	part.TopSurface = Enum.SurfaceType.Smooth
	part.BottomSurface = Enum.SurfaceType.Smooth
	part.Color = color
	part.Size = size
	part.CFrame = cframe
	part.Parent = parent
	return part
end

-- Builds the level under Workspace.SkyboundWorld (replacing a previous build) and returns the folder.
function LevelBuilder.build(level: LevelData): Model
	local existing = Workspace:FindFirstChild("SkyboundWorld")
	if existing then
		existing:Destroy()
	end

	local origin = GameConfig.WorldOrigin
	local depth = GameConfig.BlockDepth

	local root = Instance.new("Model")
	root.Name = "SkyboundWorld"
	-- Always send the whole level to every client, even with StreamingEnabled.
	root.ModelStreamingMode = Enum.ModelStreamingMode.Persistent

	local blocks = Instance.new("Folder")
	blocks.Name = "Blocks"
	blocks.Parent = root

	local minX, minY, maxX, maxY = math.huge, math.huge, -math.huge, -math.huge
	for _, block in level.blocks do
		local center = origin + Vector3.new(block.x + block.w / 2, block.y + block.h / 2, 0)
		local color = KIND_COLORS[block.kind] or KIND_COLORS.Platform
		makePart(block.name, Vector3.new(block.w, block.h, depth), CFrame.new(center), color, blocks)

		-- Ground blocks get a thin grass strip on top, so walkable tops are easy to read.
		if block.kind == "Ground" then
			local grass = 0.4
			local grassCenter = origin + Vector3.new(block.x + block.w / 2, block.y + block.h - grass / 2, 0.01)
			makePart(block.name .. "_Grass", Vector3.new(block.w, grass, depth + 0.02), CFrame.new(grassCenter), GRASS_COLOR, blocks)
		end

		minX = math.min(minX, block.x)
		minY = math.min(minY, block.y)
		maxX = math.max(maxX, block.x + block.w)
		maxY = math.max(maxY, block.y + block.h)
	end

	-- Flat sky backdrop behind everything (hides whatever else exists in the place).
	local margin = 200
	local backdropCenter = origin + Vector3.new((minX + maxX) / 2, (minY + maxY) / 2, -depth / 2 - 30)
	local backdrop = makePart(
		"Backdrop",
		Vector3.new(maxX - minX + margin * 2, maxY - minY + margin * 2, 1),
		CFrame.new(backdropCenter),
		SKY_COLOR,
		root
	)
	backdrop.Material = Enum.Material.SmoothPlastic

	root.Parent = Workspace
	return root
end

return LevelBuilder
