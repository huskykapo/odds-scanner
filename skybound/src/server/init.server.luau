--!strict
--[[
	SkyboundServer — server entry point (Rojo turns this folder into a Script in ServerScriptService).

	Startup order:
	  1. Turn off Roblox avatars (we draw our own character).
	  2. Create the remote events.
	  3. Build the level's visible parts.
	  4. Start tracking player movement.
]]

local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local Net = require(Shared.Net)

local LevelBuilder = require(script.LevelBuilder)
local PlayerStateService = require(script.PlayerStateService)

-- 1. No Roblox avatars.
if GameConfig.UseCustomCharacter then
	Players.CharacterAutoLoads = false
	-- In Studio "Play", a player can join (and get an avatar) before this script runs.
	local function removeCharacter(player: Player)
		if player.Character then
			player.Character:Destroy()
		end
		player.CharacterAdded:Connect(function(character)
			task.defer(character.Destroy, character)
		end)
	end
	Players.PlayerAdded:Connect(removeCharacter)
	for _, player in Players:GetPlayers() do
		removeCharacter(player)
	end
end

-- 2. Remotes
Net.createRemotes()

-- 3. Level visuals
local levelModule = Shared.Levels:FindFirstChild(GameConfig.ActiveLevel)
assert(levelModule, "[Skybound] Level not found: " .. GameConfig.ActiveLevel)
local level = require(levelModule) :: any
LevelBuilder.build(level)

-- 4. Movement tracking
PlayerStateService.start(level)

print(("[Skybound] Server ready: level '%s'"):format(level.displayName))
