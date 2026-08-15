--------------------------------------------------
-- Immortal TCP server for MG400
-- Self-restarting using pcall + outer while loop
--------------------------------------------------

local ip   = "192.168.1.6"
local port = 6001

-- This function runs ONE lifetime of the TCP server.
-- The outer while-true + pcall() will restart it if it exits or errors.
function run_tcp_server()
    local err   = 0
    local socket = 0

    --------------------------------------------------
    -- Create + start TCP server
    --------------------------------------------------
    err, socket = TCPCreate(true, ip, port)
    if err ~= 0 then
        print("Create failed " .. err)
        return
    end

    err = TCPStart(socket, 0)
    if err ~= 0 then
        print("Start failed " .. err)
        TCPDestroy(socket)
        return
    end

    print("TCP server started on " .. ip .. ":" .. port)

    --------------------------------------------------
    -- Main command loop
    --------------------------------------------------
    while true do
        local RecBuf
        err, RecBuf = TCPRead(socket, 100, "string")

        if err ~= 0 then
            print("Read error " .. err)
            break
        end

        local command = RecBuf.buf
        print("Received Command: " .. command)

        local responded = false

        --------------------------------------------------
        -- Relative move: "move X Y Z"
        --------------------------------------------------
        do
            local x, y, z = string.match(command, "move (%-?%d+%.?%d*) (%-?%d+%.?%d*) (%-?%d+%.?%d*)")
            if x and y and z then
                RelMovL({tonumber(x), tonumber(y), tonumber(z), 0}, {CP=0, SpeedL=20, AccL=20})
                print("RelMovL: X="..x.." Y="..y.." Z="..z)
                TCPWrite(socket, "ok")
                responded = true
            end
        end

        --------------------------------------------------
        -- Absolute move: "movj X Y Z R"
        --------------------------------------------------
        do
            local a, b, c, r = string.match(command, "movj (%-?%d+%.?%d*) (%-?%d+%.?%d*) (%-?%d+%.?%d*) (%-?%d+%.?%d*)")
            if a and b and c and r then
                local P = {coordinate = {tonumber(a), tonumber(b), tonumber(c), tonumber(r)}, user = 0, tool = 0}
                MovL(P)
                print("MovJ: X="..a.." Y="..b.." Z="..c.." R="..r)
                TCPWrite(socket, "ok")
                responded = true
            end
        end

        --------------------------------------------------
        -- Read current pose: "getpose"
        --------------------------------------------------
        if not responded and string.match(command, "^getpose") then
            local pose = GetPose()
            local pose_str = string.format("pose %.2f %.2f %.2f %.2f", pose[1], pose[2], pose[3], pose[4])
            TCPWrite(socket, pose_str)
            print("Sent pose: " .. pose_str)
            responded = true
        end

        --------------------------------------------------
        -- Digital output control: "do <index> <on/off>"
        --------------------------------------------------
        do
            local idx, state = string.match(command, "do (%d+) (%a+)")
            if idx and state then
                local output = tonumber(idx)
                local set = nil
                state = string.lower(state)

                if state == "on" then
                    set = 1
                elseif state == "off" then
                    set = 0
                end

                if set ~= nil then
                    DO(output, set)
                    print("Set DO[" .. output .. "] = " .. set)
                    local resp = string.format("do %d %d", output, set)
                    TCPWrite(socket, resp)
                else
                    print("Invalid DO state: " .. state)
                    TCPWrite(socket, "error invalid_do_state")
                end
                responded = true
            end
        end

        --------------------------------------------------
        -- Digital input read: "di <index>"
        -- Returns: "di <index> <value>"
        --------------------------------------------------
        do
            local di_idx = string.match(command, "di (%d+)")
            if di_idx then
                local input = tonumber(di_idx)
                local value = DI(input)
                local resp = string.format("di %d %d", input, value)
                TCPWrite(socket, resp)
                print("Read DI[" .. input .. "] = " .. value)
                responded = true
            end
        end

        --------------------------------------------------
        -- Unknown command
        --------------------------------------------------
        if not responded then
            TCPWrite(socket, "error unknown_command")
            print("Unknown command: " .. command)
        end

        Wait(100)
    end

    --------------------------------------------------
    -- Clean up
    --------------------------------------------------
    TCPDestroy(socket)
    print("TCP server stopped, exiting run_tcp_server()")
end

------------------------------------------------------
-- IMMORTAL WRAPPER: restart TCP server on any exit
------------------------------------------------------
while true do
    local ok, err = pcall(run_tcp_server)

    if not ok then
        print("run_tcp_server crashed: " .. tostring(err))
    else
        print("run_tcp_server exited normally, restarting...")
    end

    -- Small delay so we don't tight-loop on persistent errors
    Wait(200)
end