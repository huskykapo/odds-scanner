--[[
	Compiles every .luau file in src/ to catch syntax errors without opening Roblox Studio.
	(This checks syntax only, not types or Roblox API usage.)

	    lune run tests/compile
]]

local fs = require("@lune/fs")
local luau = require("@lune/luau")
local process = require("@lune/process")

local failures = 0
local count = 0

local function walk(path: string)
	for _, entry in fs.readDir(path) do
		local full = path .. "/" .. entry
		if fs.isDir(full) then
			walk(full)
		elseif string.sub(entry, -5) == ".luau" then
			count += 1
			local ok, err = pcall(luau.compile, fs.readFile(full))
			if not ok then
				failures += 1
				print("FAIL " .. full .. "\n     " .. tostring(err))
			end
		end
	end
end

walk("src")
print(("%d files compiled, %d with errors"):format(count, failures))
if failures > 0 then
	process.exit(1)
end
