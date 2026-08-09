import serial
import serial.tools.list_ports


def serconnect(port=None):
    global ser
    ports = serial.tools.list_ports.comports()
    if not ports:
        raise Exception("No COM ports found.")

    if isinstance(port, str):
        match = next((p for p in ports if str(p.device) == port), None)
        if not match:
            raise Exception(f"Port '{port}' not found.")
        serialport = str(match.device)
    else:
        idx = int(port) if port is not None else 0
        if idx >= len(ports):
            raise Exception("Port index out of range.")
        serialport = str(ports[idx].device)

    ser = serial.Serial(serialport, baudrate=115200, timeout=30)
    ser.readline()
    print("Connected to " + serialport)
    return ser

def setdirection(ser,axis,direction):
    if direction == "Towards":
        direction = 1
    elif direction == "Away":
        direction = 0

    if axis == "Horz":
        expected_message = 'Turnt1'
        writestring = '<M'+str(direction)+'>' #M is defined to set motor 1 direction in Arduino
        bytestowrite = writestring.encode() #encodes the string to UTF-8
        ser.write(bytestowrite) # sending the data
        ser.readline()

    if axis == "Vert":
        expected_message = 'Turnt2'
        writestring = '<N'+str(direction)+'>' #M is defined to set motor 1 direction in Arduino
        bytestowrite = writestring.encode() #encodes the string to UTF-8
        ser.write(bytestowrite) # sending the data
        ser.readline()

def setstep(ser,stepsH,stepsV):
    writestring = "<B" + str(stepsH) + "," + str(stepsV) + ">"
    bytestowrite = writestring.encode()  # encodes the string to UTF-8
    ser.write(bytestowrite)  # sending the data
    ser.readline()

def movez(ser,direction,distance,wait):
    if direction == "Up":
        direction = 1
    elif direction == "Down":
        direction = 0
    writestring = "<X" + str(direction) + "," + str(distance) + "," + str(wait) + ">"
    bytestowrite = writestring.encode()  # encodes the string to UTF-8
    ser.write(bytestowrite)  # sending the data
    ser.readline()

def move(ser,dirH,dirV,stepsH,stepsV):
    setdirection(ser,"Vert", dirV)
    setdirection(ser,"Horz", dirH)
    setstep(ser,stepsH,stepsV)

def home(ser):
    #Home Axes
    move(ser,"Away","Away", 100, 100)
    print("Home")
    writestring = '<H>' #M is defined to set motor 1 direction in Arduino
    bytestowrite = writestring.encode() #encodes the string to UTF-8
    ser.write(bytestowrite) # sending the data
    ser.readline()
    setdirection(ser,"Vert","Away")
    setdirection(ser,"Horz","Away")

def homeandfirst(ser,wpprev,wpcurrent,wptype):
    print("To home and first well")
    home(ser)
    if wptype == "384":
        vstep = ((1810/2)/15)*(wpcurrent[0]-wpprev[0])
        hstep = ((2750/2)/23)*(wpcurrent[1]-wpprev[1])
        setstep(ser,(790/2)+hstep,(2885/2)+vstep)
    elif wptype == "96":
        vstep = ((1675/2)/7)*(wpcurrent[0]-wpprev[0])
        hstep = ((2637.5/2)/11)*(wpcurrent[1]-wpprev[1]) 
        setstep(ser,(970/2)+hstep,(2940/2)+vstep)


def homeandfirstintake(ser,wpprev,wpcurrent,wptype):
    print("To home and first well (for intake setup)")
    home(ser)
    if wptype == "384":
        vstep = (1810/15)*(wpcurrent[0]-wpprev[0]) 
        hstep = (2750/23)*(wpcurrent[1]-wpprev[1]) 
        setstep(ser,805+hstep,2880+vstep)
    if wptype == "96":
        vstep = (1675/7)*(wpcurrent[0]-wpprev[0])
        hstep = (2637.5/11)*(wpcurrent[1]-wpprev[1]) 
        setstep(ser,705+hstep,820+vstep)

def nextwell(ser,wpprev,wpcurrent,wptype): #current is the next one, prev is the current lol
    if wptype == "384":
        vstep = ((1810/2)/15)*(wpcurrent[0]-wpprev[0]) 
        hstep = ((2750/2)/23)*(wpcurrent[1]-wpprev[1]) 
    if wptype == "96":
        vstep = ((1675/2)/7)*(wpcurrent[0]-wpprev[0])
        hstep = ((2637.5/2)/11)*(wpcurrent[1]-wpprev[1]) 
    print(vstep,hstep)
    if vstep > 0:
        dirV = "Away"
    else:
        dirV = "Towards"
    if hstep > 0:
        dirH = "Away"
    else:
        dirH = "Towards"    
    move(ser,dirH,dirV,hstep,vstep)


def flowswitch(ser,state): #State is 0 or 1 
    writestring = '<F'+str(state)+'>' #M is defined to set motor 1 direction in Arduino
    bytestowrite = writestring.encode() #encodes the string to UTF-8
    ser.write(bytestowrite) # sending the data

def flowswitch2(ser,state): #State is 0 or 1 
    writestring = '<U'+str(state)+'>' #M is defined to set motor 1 direction in Arduino
    bytestowrite = writestring.encode() #encodes the string to UTF-8
    ser.write(bytestowrite) # sending the data

def servoswitch(ser,state): #State is 0 or 1 
        writestring = '<S'+str(state)+'>' #M is defined to set motor 1 direction in Arduino
        bytestowrite = writestring.encode() #encodes the string to UTF-8
        ser.write(bytestowrite) # sending the data

def rotservoswitch(ser,state): #State is 0 or 1 
        writestring = '<R'+str(state)+'>' #M is defined to set motor 1 direction in Arduino
        bytestowrite = writestring.encode() #encodes the string to UTF-8
        ser.write(bytestowrite) # sending the data

def setZdirection(ser,direction):
    if direction == "Towards":
        direction = 1
    elif direction == "Away":
        direction = 0
    
    writestring = '<O'+str(direction)+'>' #M is defined to set motor 1 direction in Arduino
    bytestowrite = writestring.encode() #encodes the string to UTF-8
    ser.write(bytestowrite) # sending the data
    ser.readline()    

def setZstepcw(ser,steps):
    writestring = "<Z"+str(steps)+">"
    bytestowrite = writestring.encode()  # encodes the string to UTF-8
    ser.write(bytestowrite)  # sending the data

def setZstepacw(ser,steps):
    writestring = "<O"+str(steps)+">"
    bytestowrite = writestring.encode()  # encodes the string to UTF-8
    ser.write(bytestowrite)  # sending the data

def set_servo_angle(ser, servo_number, angle):
    servo_number = int(servo_number)
    angle = int(max(0, min(180, angle)))  # clamp 0..180

    # Arduino expects: <A,servoNumber,angle>
    writestring = f"<A,{servo_number},{angle}>"
    ser.write(writestring.encode())
    try:
        if getattr(ser, "in_waiting", 0):
            ser.readline()
    except Exception:
        pass

