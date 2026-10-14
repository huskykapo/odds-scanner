--!strict
--[[
	CollisionWorld — static 2D collision geometry made of axis-aligned rectangles ("boxes").

	Why our own collision instead of Roblox physics or raycasts?
	  * A precision platformer needs the exact same result every time. Roblox physics (and
	    Humanoid jumping) adds forces, friction and network ownership effects we cannot fully control.
	  * Level geometry here is rectangles in one plane, so pure 2D box tests are exact, cheap,
	    and identical on the client and the server (the server reuses this module for validation).
	  * The module is plain Luau, so it can be unit-tested outside Roblox Studio.

	Boxes never move. Lookups use a uniform grid ("spatial hash") so only nearby boxes are tested.

	Overlap is STRICT: boxes that merely touch (share an edge) do not overlap. The movement code
	relies on this so a body standing exactly on a floor is not "inside" it.
]]

local CollisionWorld = {}
CollisionWorld.__index = CollisionWorld

export type Box = {
	id: number,
	minX: number,
	minY: number,
	maxX: number,
	maxY: number,
	-- Free-form label from level data, e.g. "Ground", "Platform", "Goal".
	kind: string,
	-- Used internally to avoid returning one box twice from a single query.
	stamp: number,
}

export type CollisionWorld = typeof(setmetatable(
	{} :: {
		cellSize: number,
		boxes: { Box },
		cells: { [number]: { [number]: { Box } } },
		queryStamp: number,
	},
	CollisionWorld
))

function CollisionWorld.new(cellSize: number?): CollisionWorld
	local self = setmetatable({
		cellSize = cellSize or 16,
		boxes = {},
		cells = {},
		queryStamp = 0,
	}, CollisionWorld)
	return self
end

function CollisionWorld.addBox(self: CollisionWorld, minX: number, minY: number, maxX: number, maxY: number, kind: string?): Box
	assert(maxX > minX and maxY > minY, "CollisionWorld.addBox: box must have positive width and height")
	local box: Box = {
		id = #self.boxes + 1,
		minX = minX,
		minY = minY,
		maxX = maxX,
		maxY = maxY,
		kind = kind or "Solid",
		stamp = 0,
	}
	table.insert(self.boxes, box)

	local size = self.cellSize
	for cx = math.floor(minX / size), math.floor(maxX / size) do
		local column = self.cells[cx]
		if not column then
			column = {}
			self.cells[cx] = column
		end
		for cy = math.floor(minY / size), math.floor(maxY / size) do
			local cell = column[cy]
			if not cell then
				cell = {}
				column[cy] = cell
			end
			table.insert(cell, box)
		end
	end
	return box
end

--[[
	Finds every box that strictly overlaps the rectangle (minX, minY)-(maxX, maxY).
	Results are written into `out` (cleared first) to avoid creating a new table per query.
	Returns the number of boxes found.
]]
function CollisionWorld.query(self: CollisionWorld, minX: number, minY: number, maxX: number, maxY: number, out: { Box }): number
	table.clear(out)
	self.queryStamp += 1
	local stamp = self.queryStamp
	local size = self.cellSize
	local count = 0
	for cx = math.floor(minX / size), math.floor(maxX / size) do
		local column = self.cells[cx]
		if column then
			for cy = math.floor(minY / size), math.floor(maxY / size) do
				local cell = column[cy]
				if cell then
					for _, box in cell do
						if box.stamp ~= stamp then
							box.stamp = stamp
							if minX < box.maxX and maxX > box.minX and minY < box.maxY and maxY > box.minY then
								count += 1
								out[count] = box
							end
						end
					end
				end
			end
		end
	end
	return count
end

local scratch: { Box } = {}

-- Convenience: true if anything overlaps the rectangle.
function CollisionWorld.isBlocked(self: CollisionWorld, minX: number, minY: number, maxX: number, maxY: number): boolean
	return self:query(minX, minY, maxX, maxY, scratch) > 0
end

return CollisionWorld
