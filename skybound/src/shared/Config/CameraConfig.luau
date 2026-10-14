--!strict
--[[
	CameraConfig — the fixed side-view camera.

	Roblox has no true orthographic camera. We approximate one by placing a perspective camera
	far away with a narrow field of view: perspective distortion shrinks as the FOV narrows,
	so platform edges look almost flat and jump distances read correctly on screen.
	The camera distance is computed automatically from FieldOfView and VisibleHeight.
]]

local CameraConfig = {
	-- Vertical field of view in degrees. Smaller = flatter (more 2D-looking) but the camera sits
	-- further away. 15–25 is a good range.
	FieldOfView = 20,
	-- How many studs of the level are visible vertically, on every screen size.
	VisibleHeight = 48,
	-- On narrow (portrait phone) screens, zoom out so at least this many studs fit horizontally.
	MinVisibleWidth = 40,

	-- "Follow": smoothly follow the player vertically.
	-- "Screens": the level is split into stacked screens of VisibleHeight; the camera moves one
	--            whole screen at a time (like classic single-screen climbers).
	VerticalMode = "Follow" :: "Follow" | "Screens",
	-- "Fixed": keep the level's camera centre X (set per level) — best for levels about one screen wide.
	-- "Follow": follow the player horizontally.
	HorizontalMode = "Fixed" :: "Fixed" | "Follow",

	-- Follow mode: aim the camera this far ABOVE the player, so upcoming platforms are visible.
	LookAheadUp = 6,
	-- Follow-speed (higher = tighter). The camera closes about 1 - e^(-Sharpness * dt) of the gap per frame.
	FollowSharpness = 6,
	-- Used instead when the player is far below the camera target (fast falls), so the camera catches up.
	FallCatchUpSharpness = 14,
	-- The player is never allowed closer than this to the top/bottom screen edge (hard limit).
	EdgeMargin = 6,
	-- Screens mode: how quickly the camera slides to the next screen.
	ScreenTransitionSharpness = 18,
}

return CameraConfig
