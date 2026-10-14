--!strict
--[[
	AudioController — plays sound effects for movement events.

	PLACEHOLDER SOUNDS: these use sound files that ship inside the Roblox client
	("rbxasset://sounds/..."), so no uploads are needed for testing. Replace them with our own
	original sounds later (upload to Roblox, then put the rbxassetid:// IDs here).
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local SoundService = game:GetService("SoundService")

local Shared = ReplicatedStorage:WaitForChild("Skybound")
local GameConfig = require(Shared.Config.GameConfig)
local MovementConfig = require(Shared.Config.MovementConfig)

type SoundDef = { id: string, volume: number, speed: number }

-- event kind -> sound
local SOUNDS: { [string]: SoundDef } = {
	Jump = { id = "rbxasset://sounds/action_jump.mp3", volume = 0.6, speed = 1 },
	Land = { id = "rbxasset://sounds/action_jump_land.mp3", volume = 0.7, speed = 1 },
	BumpWall = { id = "rbxasset://sounds/action_jump_land.mp3", volume = 0.5, speed = 1.6 },
	BumpCeiling = { id = "rbxasset://sounds/action_jump_land.mp3", volume = 0.5, speed = 1.8 },
	Splat = { id = "rbxasset://sounds/action_jump_land.mp3", volume = 1, speed = 0.6 },
}

local AudioController = {}
AudioController.__index = AudioController

export type AudioController = typeof(setmetatable(
	{} :: {
		folder: Folder,
		sounds: { [string]: Sound },
	},
	AudioController
))

function AudioController.new(): AudioController
	local folder = Instance.new("Folder")
	folder.Name = "SkyboundSfx"
	local sounds = {}
	for kind, def in SOUNDS do
		local sound = Instance.new("Sound")
		sound.Name = kind
		sound.SoundId = def.id
		sound.PlaybackSpeed = def.speed
		sound.Parent = folder
		sounds[kind] = sound
	end
	folder.Parent = SoundService -- not under a Part, so it plays as non-positional ("2D") sound
	return setmetatable({ folder = folder, sounds = sounds }, AudioController)
end

function AudioController.playEvent(self: AudioController, kind: string, value: number)
	local sound = self.sounds[kind]
	if not sound then
		return
	end
	-- A landing after a "Splat" is already loud enough; skip the extra landing thud.
	if kind == "Land" and MovementConfig.LongFallStunTime > 0 and value >= MovementConfig.LongFallStunDistance then
		return
	end
	sound.Volume = SOUNDS[kind].volume * GameConfig.Audio.SfxVolume
	sound.TimePosition = 0
	sound:Play()
end

function AudioController.destroy(self: AudioController)
	self.folder:Destroy()
end

return AudioController
