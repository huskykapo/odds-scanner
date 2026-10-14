--!strict
--[[
	TestLevel — a small room for testing and tuning movement.

	Every block is a rectangle: (x, y) is its BOTTOM-LEFT corner, w/h its width/height, in studs.
	The floor's top surface is at y = 0. Gameplay ignores depth (Z).

	Contents (left to right on the floor, then the climb):
	  * flat ground with the spawn point
	  * TallPillar   — an 8-stud wall, only clearable with a near-full jump
	  * LowCeiling   — a slab you can walk under; jumping under it bumps your head
	  * NarrowPost   — a 1-stud-wide post to land on (the body is 2 studs wide)
	  * Pit          — a wide gap with a lower floor; jump across or climb out
	  * Ceiling      — a low ceiling above step 1 (straight-up jumps hit it)
	  * Overhang     — blocks the obvious jump to the right from step 4
	  * Step1..Step7 — the climbing route, ending at the gold Summit
	  * walking off the right side of the Summit is a long fall back to the pit (tests "splat")

	`route` lists the platforms in climbing order. The automated tests (tests/run.luau) check that
	every step of the route is reachable with the current MovementConfig values.
]]

local TestLevel = {
	id = "TestLevel",
	displayName = "Movement Test Room",
	spawn = { x = -12, y = 0 },
	cameraCenterX = 0,

	blocks = {
		-- Boundary walls
		{ name = "WallLeft", kind = "Wall", x = -34, y = -12, w = 2, h = 84 },
		{ name = "WallRight", kind = "Wall", x = 32, y = -12, w = 2, h = 84 },

		-- Floor, with a pit between x = 14 and x = 22
		{ name = "FloorLeft", kind = "Ground", x = -32, y = -12, w = 46, h = 12 },
		{ name = "PitFloor", kind = "Ground", x = 14, y = -12, w = 8, h = 6 },
		{ name = "FloorRight", kind = "Ground", x = 22, y = -12, w = 10, h = 12 },

		-- Ground-level test fixtures
		{ name = "TallPillar", kind = "Wall", x = -22, y = 0, w = 2, h = 8 },
		{ name = "LowCeiling", kind = "Platform", x = -6, y = 4, w = 12, h = 1.5 },
		{ name = "NarrowPost", kind = "Platform", x = 8, y = 0, w = 1, h = 3 },
		{ name = "Ceiling", kind = "Wall", x = 24, y = 17, w = 8, h = 1.5 },
		{ name = "Overhang", kind = "Wall", x = -6, y = 31, w = 8, h = 2 },

		-- Climbing route
		{ name = "Step1", kind = "Platform", x = 26, y = 5, w = 6, h = 1 },
		{ name = "Step2", kind = "Platform", x = 14, y = 12, w = 5, h = 1 },
		{ name = "Step3", kind = "Platform", x = 2, y = 19, w = 6, h = 1 },
		{ name = "Step4", kind = "Platform", x = -10, y = 25, w = 4, h = 1 },
		{ name = "Step5", kind = "Platform", x = -24, y = 31, w = 6, h = 1 },
		{ name = "Step6", kind = "Platform", x = -14, y = 36, w = 5, h = 1 },
		{ name = "Step7", kind = "Platform", x = -4, y = 43, w = 6, h = 1 },
		{ name = "Summit", kind = "Goal", x = 8, y = 49, w = 12, h = 1 },
	},

	route = { "FloorLeft", "FloorRight", "Step1", "Step2", "Step3", "Step4", "Step5", "Step6", "Step7", "Summit" },
}

return TestLevel
