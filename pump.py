# Install packages for pressure devices.
import sys
sys.path.append('./DLL64')
sys.path.append('./Elveflow64.py')
from ctypes import *
from array import array
from Elveflow64 import *
Instr_ID = c_int32()
import numpy as np
import ctypes as ct


def pressure_init(device_id=""):
    if not device_id:
        raise ValueError("An OB1 device ID must be provided in the microfluidics config.")
    error = OB1_Initialization(device_id.encode('ascii'),2,2,2,2,byref(Instr_ID))
    print('error:%d' % error)
    print("OB1 ID: %d" % Instr_ID.value)
    return abs(error)

def sensor_init(sensor1, sensor2, sensor3, sensor4):
    print(f"[pump.sensor_init] Received configs: s1={sensor1}, s2={sensor2}, s3={sensor3}, s4={sensor4}")
    if sensor1 != None:
        error1=OB1_Add_Sens(Instr_ID, sensor1[0], sensor1[1], sensor1[2], sensor1[3], 7, 0)
    else:
        error1 = 0
    if sensor2 != None:
        error2=OB1_Add_Sens(Instr_ID, sensor2[0], sensor2[1], sensor2[2], sensor2[3], 7, 0)
    else:
        error2 = 0
    if sensor3 != None:
        error3=OB1_Add_Sens(Instr_ID, sensor3[0], sensor3[1], sensor3[2], sensor3[3], 7, 0)
    else:
        error3 = 0
    if sensor4 != None:
        error4=OB1_Add_Sens(Instr_ID, sensor4[0], sensor4[1], sensor4[2], sensor4[3], 7, 0)
    else:
        error4 = 0

    print('error add digit flow sensor:',error1,error2,error3,error4)
    error = np.max([abs(error1),abs(error2),abs(error3),abs(error4)])
    return error

def pressure_calib(answer):
    print("Pressure calib")
    Calib = (c_double*1000)()
    while True:
        if answer == 'default':
            error = Elveflow_Calibration_Default (byref(Calib),1000)
            calibarr = byref(Calib)
            print("Default Calibration taken")
            break
        if answer == 'load':
            #error = Elveflow_Calibration_Load (Calib_path.encode('ascii'), byref(Calib), 1000)
            array = np.load("./calib.npy", allow_pickle=True)
            array.ctypes.data
            calibarr = array.ctypes.data_as(ct.POINTER(ct.c_double*1000))
            print(calibarr)
            error = 0
            break
        if answer == 'new':
            OB1_Calib (Instr_ID.value, Calib, 1000)
            error = 0
            calibarr = Calib
            np.save("./calib.npy",Calib)
            print('Calib saved in calib.npy')
            break
    return calibarr,error

def set_pressure(set_channel,set_pressure,calibarr):
    set_channel=int(set_channel) # convert to int
    set_channel=c_int32(set_channel) # convert to c_int32
    set_pressure=float(set_pressure) 
    set_pressure=c_double(set_pressure) # convert to c_double
    error=OB1_Set_Press(Instr_ID.value, set_channel, set_pressure, calibarr ,1000) 
    return error 

def get_sensor_data(sensor_channel):
    data_sens=c_double()
    set_channel=int(sensor_channel) # convert to int
    set_channel=c_int32(sensor_channel) # convert to c_int32
    error=OB1_Get_Sens_Data(Instr_ID.value,set_channel, 1,byref(data_sens)) # Acquire_data=1 -> read all the analog values
    if error != 0:
        print(f"[pump.get_sensor_data] Channel {sensor_channel}: data={data_sens.value:.6f}, error={error}")
    return data_sens.value, error

def get_pressure_data(press_channel,calibarr):
    set_channel=c_int32( int(press_channel) ) # convert to c_int32
    get_pressure=c_double()
    error=OB1_Get_Press(Instr_ID.value, set_channel, 1, calibarr ,byref(get_pressure), 1000) # Acquire_data=1 -> read all the analog values  #byref(Calib)
    return get_pressure.value, error

#For rotary valve

def MUX_DRI_init(port):
    global Instr_ID
    if not port:
        raise ValueError("A MUX DRI port must be provided, e.g. ASRL<port>::INSTR.")
    Instr_ID=c_int32()
    error=MUX_DRI_Initialization(str(port).encode('ascii'),byref(Instr_ID))
    MUX_DRI_ID = Instr_ID.value
    print('error:%d' % error)
    print("MUX DRI ID: %d" % Instr_ID.value)    
    return(MUX_DRI_ID,error)

def MUX_DRI_getvalve():
    valve=c_int32(-1)
    error=MUX_DRI_Get_Valve(Instr_ID.value,byref(valve)) #get the active valve. it returns 0 if valve is busy.
    print('selected channel',valve.value)   
    return(valve.value,error)

def MUX_DRI_setvalve(position):
    Valve2=int(position)#convert to int
    Valve2=c_int32(Valve2)#convert to c_int32
    error=MUX_DRI_Set_Valve(Instr_ID.value,Valve2,0) #you can select valve rotation way, either shortest, clockwise or counter clockwise (only for MUX Distribution and Recirculation)
    return(error)

def MUX_DRI_homevalve():
    Answer=(c_char*40)()
    #send the command to Home the valve (only for MUX Distribution and Recirculation)
    #Home the valve can take several seconds. Wait for the end of the valve movement to be able to set a new valve position.
    error=MUX_DRI_Send_Command(Instr_ID.value,0,Answer,40) #length is set to 40 to contain the whole Serial Number
    print('Answer',Answer.value)
    return(error)

#For valve manifold

def MUX_init(device=""):
    global Instr_ID
    if not device:
        raise ValueError("A MUX device name must be provided, e.g. Dev1.")
    Instr_ID=c_int32()
    error=MUX_Initialization(str(device).encode('ascii'),byref(Instr_ID))
    MUX_DRI_ID = Instr_ID.value
    print('error:%d' % error)
    print("MUX DRI ID: %d" % Instr_ID.value)    
    return(MUX_DRI_ID,error)

def MUX_setall(val):
    if val == 0:
        valve_state=[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
    elif val == 1:
        valve_state = [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1]
    valve_state_c = np.array(valve_state, dtype=np.int32)
    CArrayType = ct.c_long * len(valve_state)
    ctypes_array = CArrayType(*valve_state_c)
    error=MUX_Set_all_valves(Instr_ID.value, ctypes_array, 16)
    #print(valve_state)
    return valve_state

def MUX_setone(valve_state,port,val):
    valve_state[port] = val
    valve_state_c = np.array(valve_state, dtype=np.int32)
    CArrayType = ct.c_long * len(valve_state)
    ctypes_array = CArrayType(*valve_state_c)
    #print(np.ctypeslib.as_array(valve_state))
    error=MUX_Set_all_valves(Instr_ID.value, ctypes_array, 16)
    return valve_state
