--!strict
--[[
	LevelGeometry — turns level data (a list of rectangles) into a CollisionWorld.

	Both the client (for movement) and the server (for validation) call this with the same data,
	so they agree exactly on where every surface is. Building collision from data rather than from
	Parts in the Workspace also means collision never depends on which Parts have streamed in.
]]

local CollisionWorld = require(script.Parent.Parent.Movement.CollisionWorld)

export type BlockData = {
	name: string,
	kind: string,
	x: number, -- bottom-left corner
	y: number,
	w: number,
	h: number,
}

export type LevelData = {
	id: string,
	displayName: string,
	spawn: { x: number, y: number },
	cameraCenterX: number,
	blocks: { BlockData },
	route: { string }?,
}

local LevelGeometry = {}

function LevelGeometry.buildWorld(level: LevelData): (CollisionWorld.CollisionWorld, { [string]: CollisionWorld.Box })
	local world = CollisionWorld.new(16)
	local byName: { [string]: CollisionWorld.Box } = {}
	for _, block in level.blocks do
		local box = world:addBox(block.x, block.y, block.x + block.w, block.y + block.h, block.kind)
		byName[block.name] = box
	end
	return world, byName
end

return LevelGeometry
