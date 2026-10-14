--!strict
--[[
	Net — remote event definitions and the compact binary format for movement state.

	Remotes live in ReplicatedStorage.SkyboundRemotes, created by the server at startup.
	(Kept outside the Rojo-synced "Skybound" folder so live sync never touches them.)

	Movement state is packed into a `buffer` instead of a table: it is much smaller on the wire,
	and UnreliableRemoteEvent payloads are limited to about 900 bytes.
]]

local ReplicatedStorage = game:GetService("ReplicatedStorage")

local FOLDER_NAME = "SkyboundRemotes"

local Net = {}

-- name -> class
Net.Definitions = {
	StateUpdate = "UnreliableRemoteEvent", -- client -> server: my movement state (buffer)
	PlayerStates = "UnreliableRemoteEvent", -- server -> clients: everyone's state (buffer)
	RequestRespawn = "RemoteEvent", -- client -> server: please put me back at the start
	Respawn = "RemoteEvent", -- server -> client: (x, y) where you must restart
}

-- Movement modes as small numbers for the wire.
Net.ModeToCode = { Ground = 1, Charging = 2, Air = 3, Stunned = 4 } :: { [string]: number }
Net.CodeToMode = { "Ground", "Charging", "Air", "Stunned" } :: { string }

-- Called once by the server.
function Net.createRemotes()
	local folder = ReplicatedStorage:FindFirstChild(FOLDER_NAME)
	if not folder then
		folder = Instance.new("Folder")
		folder.Name = FOLDER_NAME
		folder.Parent = ReplicatedStorage
	end
	for name, className in Net.Definitions do
		if not folder:FindFirstChild(name) then
			local remote = Instance.new(className)
			remote.Name = name
			remote.Parent = folder
		end
	end
end

-- Server or client: returns a remote by name (waits for the server to create it).
function Net.get(name: string): any
	assert(Net.Definitions[name], "Net.get: unknown remote " .. name)
	local folder = ReplicatedStorage:WaitForChild(FOLDER_NAME)
	return folder:WaitForChild(name)
end

-- ── Client -> server state (19 bytes) ───────────────────────────────────────
-- f32 x, f32 y, f32 vx, f32 vy, u8 mode, i8 facing, u8 charge (0..255)
export type ReportedState = {
	x: number,
	y: number,
	vx: number,
	vy: number,
	mode: string,
	facing: number,
	charge: number, -- 0..1
}

local STATE_SIZE = 19

function Net.encodeState(x: number, y: number, vx: number, vy: number, mode: string, facing: number, charge: number): buffer
	local b = buffer.create(STATE_SIZE)
	buffer.writef32(b, 0, x)
	buffer.writef32(b, 4, y)
	buffer.writef32(b, 8, vx)
	buffer.writef32(b, 12, vy)
	buffer.writeu8(b, 16, Net.ModeToCode[mode] or 1)
	buffer.writei8(b, 17, if facing < 0 then -1 else 1)
	buffer.writeu8(b, 18, math.clamp(math.round(charge * 255), 0, 255))
	return b
end

local function isFinite(n: number): boolean
	return n == n and n ~= math.huge and n ~= -math.huge
end

-- Server side: decodes an untrusted payload. Returns nil if it is malformed.
function Net.decodeState(payload: any): ReportedState?
	if typeof(payload) ~= "buffer" or buffer.len(payload) ~= STATE_SIZE then
		return nil
	end
	local x = buffer.readf32(payload, 0)
	local y = buffer.readf32(payload, 4)
	local vx = buffer.readf32(payload, 8)
	local vy = buffer.readf32(payload, 12)
	if not (isFinite(x) and isFinite(y) and isFinite(vx) and isFinite(vy)) then
		return nil
	end
	local mode = Net.CodeToMode[buffer.readu8(payload, 16)]
	if not mode then
		return nil
	end
	return {
		x = x,
		y = y,
		vx = vx,
		vy = vy,
		mode = mode,
		facing = if buffer.readi8(payload, 17) < 0 then -1 else 1,
		charge = buffer.readu8(payload, 18) / 255,
	}
end

-- ── Server -> clients snapshot (19 bytes per player) ─────────────────────────
-- f64 userId, f32 x, f32 y, u8 mode, i8 facing, u8 charge
export type SnapshotEntry = {
	userId: number,
	x: number,
	y: number,
	mode: string,
	facing: number,
	charge: number,
}

local ENTRY_SIZE = 19
-- 40 entries * 19 bytes = 760 bytes, safely under the unreliable payload limit.
Net.MaxEntriesPerSnapshot = 40

function Net.encodeSnapshot(entries: { SnapshotEntry }, first: number, last: number): buffer
	local b = buffer.create((last - first + 1) * ENTRY_SIZE)
	local offset = 0
	for i = first, last do
		local e = entries[i]
		buffer.writef64(b, offset, e.userId)
		buffer.writef32(b, offset + 8, e.x)
		buffer.writef32(b, offset + 12, e.y)
		buffer.writeu8(b, offset + 16, Net.ModeToCode[e.mode] or 1)
		buffer.writei8(b, offset + 17, if e.facing < 0 then -1 else 1)
		buffer.writeu8(b, offset + 18, math.clamp(math.round(e.charge * 255), 0, 255))
		offset += ENTRY_SIZE
	end
	return b
end

function Net.decodeSnapshot(payload: any): { SnapshotEntry }
	local entries = {}
	if typeof(payload) ~= "buffer" or buffer.len(payload) % ENTRY_SIZE ~= 0 then
		return entries
	end
	for offset = 0, buffer.len(payload) - ENTRY_SIZE, ENTRY_SIZE do
		table.insert(entries, {
			userId = buffer.readf64(payload, offset),
			x = buffer.readf32(payload, offset + 8),
			y = buffer.readf32(payload, offset + 12),
			mode = Net.CodeToMode[buffer.readu8(payload, offset + 16)] or "Ground",
			facing = buffer.readi8(payload, offset + 17),
			charge = buffer.readu8(payload, offset + 18) / 255,
		})
	end
	return entries
end

return Net
