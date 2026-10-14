--!strict
--[[
	CameraController — fixed side-view camera.

	The camera always looks straight along -Z at the gameplay plane and never rotates.
	It is placed far away with a narrow field of view to approximate an orthographic (flat 2D)
	view — Roblox has no true orthographic camera. The distance is recomputed every frame from
	the screen shape, so the same vertical slice of the level is visible on every device
	(and portrait phones zoom out to keep a minimum width).
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local CameraConfig = require(Shared.Config.CameraConfig)
local GameConfig = require(Shared.Config.GameConfig)
local MovementConfig = require(Shared.Config.MovementConfig)
local LevelGeometry = require(Shared.Levels.LevelGeometry)

local CameraController = {}
CameraController.__index = CameraController

export type CameraController = typeof(setmetatable(
	{} :: {
		camera: Camera,
		level: LevelGeometry.LevelData,
		x: number,
		y: number,
		screensBaseY: number,
	},
	CameraController
))

function CameraController.new(camera: Camera, level: LevelGeometry.LevelData): CameraController
	local self = setmetatable({
		camera = camera,
		level = level,
		x = level.cameraCenterX,
		y = level.spawn.y,
		-- Screens mode: the first screen starts a little below the spawn point.
		screensBaseY = level.spawn.y - CameraConfig.EdgeMargin,
	}, CameraController)
	camera.CameraType = Enum.CameraType.Scriptable
	camera.FieldOfView = CameraConfig.FieldOfView
	return self
end

-- Visible height actually used this frame (larger than VisibleHeight on narrow screens).
function CameraController.getVisibleHeight(self: CameraController): number
	local viewport = self.camera.ViewportSize
	local aspect = if viewport.Y > 0 then viewport.X / viewport.Y else 16 / 9
	return math.max(CameraConfig.VisibleHeight, CameraConfig.MinVisibleWidth / aspect)
end

local function smoothing(sharpness: number, dt: number): number
	return 1 - math.exp(-sharpness * dt)
end

-- Desired camera centre Y for a player whose feet are at y.
function CameraController._targetY(self: CameraController, playerCenterY: number, visibleHeight: number): number
	if CameraConfig.VerticalMode == "Screens" then
		local index = math.floor((playerCenterY - self.screensBaseY) / visibleHeight)
		return self.screensBaseY + (index + 0.5) * visibleHeight
	end
	return playerCenterY + CameraConfig.LookAheadUp
end

function CameraController.snapTo(self: CameraController, playerX: number, playerY: number)
	local visibleHeight = self:getVisibleHeight()
	local center = playerY + MovementConfig.BodyHeight / 2
	self.y = self:_targetY(center, visibleHeight)
	self.x = if CameraConfig.HorizontalMode == "Follow" then playerX else self.level.cameraCenterX
	self:_apply(visibleHeight)
end

function CameraController.update(self: CameraController, dt: number, playerX: number, playerY: number)
	local visibleHeight = self:getVisibleHeight()
	local center = playerY + MovementConfig.BodyHeight / 2
	local target = self:_targetY(center, visibleHeight)

	local sharpness
	if CameraConfig.VerticalMode == "Screens" then
		sharpness = CameraConfig.ScreenTransitionSharpness
	elseif target < self.y - visibleHeight * 0.25 then
		sharpness = CameraConfig.FallCatchUpSharpness
	else
		sharpness = CameraConfig.FollowSharpness
	end
	self.y += (target - self.y) * smoothing(sharpness, dt)

	-- Hard limit: never let the player get closer than EdgeMargin to the top/bottom edge.
	local half = visibleHeight / 2
	local margin = math.min(CameraConfig.EdgeMargin, half - MovementConfig.BodyHeight)
	if center > self.y + half - margin then
		self.y = center - half + margin
	elseif center < self.y - half + margin then
		self.y = center + half - margin
	end

	if CameraConfig.HorizontalMode == "Follow" then
		self.x += (playerX - self.x) * smoothing(CameraConfig.FollowSharpness, dt)
	else
		self.x = self.level.cameraCenterX
	end

	self:_apply(visibleHeight)
end

function CameraController._apply(self: CameraController, visibleHeight: number)
	local camera = self.camera
	-- Other scripts (e.g. Roblox's default camera) must not take over.
	if camera.CameraType ~= Enum.CameraType.Scriptable then
		camera.CameraType = Enum.CameraType.Scriptable
	end
	if camera.FieldOfView ~= CameraConfig.FieldOfView then
		camera.FieldOfView = CameraConfig.FieldOfView
	end

	local distance = (visibleHeight / 2) / math.tan(math.rad(CameraConfig.FieldOfView / 2))
	-- Measure from the plane the characters are drawn on (just in front of the blocks).
	local planeZ = GameConfig.BlockDepth / 2
	local target = GameConfig.WorldOrigin + Vector3.new(self.x, self.y, planeZ)
	camera.CFrame = CFrame.lookAt(target + Vector3.new(0, 0, distance), target)
	camera.Focus = CFrame.new(target)
end

return CameraController
