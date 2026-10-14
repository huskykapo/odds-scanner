--[[
	RobloxShim — lets plain-Luau modules from src/ run under Lune (outside Roblox Studio).

	In Roblox, modules require each other through the Instance tree, e.g.
	    require(script.Parent.Parent.Config.MovementConfig)
	This shim builds a fake `script` object whose `.Parent` / child lookups follow the folder
	layout on disk, then loads each module with that `script` and a matching `require`.

	Only for pure modules (no Instances, no services). Roblox-only scripts are not run here.
]]

local fs = require("@lune/fs")
local luau = require("@lune/luau")

local RobloxShim = {}

local moduleCache: { [string]: any } = {}

-- Builds a table tree mirroring the folders: every node has `Parent`, and children are fields.
-- (Built eagerly because Lune's file-system calls cannot run inside an __index metamethod.)
local function buildTree(path: string, parent: any?): any
	local node: any = { __path = path, Parent = parent }
	for _, entry in fs.readDir(path) do
		local full = path .. "/" .. entry
		if fs.isDir(full) then
			node[entry] = buildTree(full, node)
		elseif string.sub(entry, -5) == ".luau" then
			local name = string.sub(entry, 1, -6)
			node[name] = { __path = full, __isFile = true, Parent = node }
		end
	end
	return node
end

local function requireNode(node: any): any
	local path = node.__path
	if moduleCache[path] ~= nil then
		return moduleCache[path]
	end
	local source = fs.readFile(path)
	local chunk = luau.load(source, {
		debugName = path,
		environment = {
			script = node,
			require = requireNode,
		},
	})
	local result = chunk()
	moduleCache[path] = result
	return result
end

-- Requires a module by its path on disk, e.g. "src/shared/Movement/MovementSimulator.luau".
function RobloxShim.require(path: string): any
	local rootPath, rest = string.match(path, "^([^/]+)/(.+)%.luau$")
	assert(rootPath and rest, "RobloxShim.require: expected a path like src/x/y.luau")
	local node = buildTree(rootPath :: string, nil)
	for segment in string.gmatch(rest :: string, "[^/]+") do
		node = node[segment]
		assert(node, "RobloxShim.require: not found: " .. path)
	end
	return requireNode(node)
end

return RobloxShim
