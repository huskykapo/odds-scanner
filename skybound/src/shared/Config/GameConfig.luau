--!strict
--[[
	GameConfig — project-wide settings that are not movement or camera physics.
]]

local GameConfig = {
	-- Which level module in Shared/Levels to load.
	ActiveLevel = "TestLevel",

	-- Where the 2D world is placed in the 3D workspace. Gameplay coordinates (x, y) are offsets from
	-- this point. It is far from the origin so our level never overlaps an existing map in the place
	-- (for example the template's own map); the camera looks from +Z towards -Z.
	WorldOrigin = Vector3.new(0, 0, -2000),
	-- Depth (Z thickness) of level blocks. Purely visual: gameplay ignores Z.
	-- Thin blocks look flatter (more 2D); characters are drawn just in front of the blocks' front faces.
	BlockDepth = 4,

	-- Placeholder character colour (until sprite skins exist).
	PlaceholderColor = Color3.fromRGB(255, 120, 90),

	-- Players get our own character instead of their Roblox avatar.
	-- This turns off Players.CharacterAutoLoads on the server.
	UseCustomCharacter = true,

	-- Networking
	StateSendRate = 20, -- client -> server position updates per second
	StateBroadcastRate = 15, -- server -> clients updates per second (other players' positions)

	-- Server-side sanity checks on reported movement (see PlayerStateService).
	Validation = {
		-- Reported speed may exceed the fastest legitimate speed by this factor before it is flagged.
		SpeedTolerance = 1.5,
		-- Extra time (seconds) allowed between packets to absorb network jitter.
		JitterAllowance = 0.25,
		-- Log a warning after this many suspicious packets in a row. (No punishment yet.)
		StrikesBeforeWarning = 5,
	},

	Audio = {
		-- 0..1. Separate music volume will come with music.
		SfxVolume = 0.5,
	},

	Debug = {
		ShowHud = true, -- F1 toggles in game
		ShowChargeMeter = true,
		AllowResetKey = true, -- R returns to the level start
	},
}

return GameConfig
