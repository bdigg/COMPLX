# COMPLX Control Software

COMPLX is the control software for a robotic-microfluidic platform for
combinatorial lipid nanoparticle formulation. It coordinates a DOBOT MG400 input
robot, pressure-driven microfluidic flow control, Arduino-compatible collection
hardware, experiment scheduling, live status updates, and run logging for
plate-based formulation workflows.

This repository accompanies the manuscript *A robotic-microfluidic platform for
combinatorial access to millions of lipid nanoparticle formulations*. It
contains the Python control software, Arduino firmware, public configuration
templates, example experiment import data, and the hardware bill of materials
in `Part Files and BOM/`.

## Platform Overview

COMPLX combines three hardware subsystems:

- DOBOT MG400 robotic input handling for loading lipid stocks and carrying out
  automated line changes.
- Elveflow OB1 pressure-driven microfluidic control with inline flow sensing.
- Arduino-controlled output hardware for plate collection and valve actuation.

The software communicates with the DOBOT over TCP/IP, with the Elveflow OB1
through the manufacturer Python interface, and with the collection stage and
servo-actuated valves through serial communication. Hardware addresses, COM
ports, calibration values, and control parameters are stored in `configs/`, so
the platform can be adapted without modifying source code.

In the workflow described in the associated paper, COMPLX is used to select from
lipid stock libraries, generate or import formulation spaces, assign output
wells in 96- or 384-well plate layouts, schedule stock changes, control
microfluidic formulation, and record full run data.

## Installation And Setup

These instructions assume the hardware has been assembled as described in the
associated manuscript and supplementary methods.

### 1. Install Conda

Install a 64-bit Conda distribution for Windows.

### 2. Create A Python Environment

Create and activate a dedicated environment:

```powershell
conda create -n complx python=3.12
conda activate complx
```

Python 3.11 or newer is expected. Python 3.12.2 was used for the manuscript
experiments.

### 3. Install Python Packages

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

### 4. Install Elveflow Software And Drivers

Install the Elveflow software, SDK, runtime components, and drivers for the OB1
controller and flow sensors:

https://elveflow.com/microfluidic-applications/setup-environment-elveflow-sdk/

Elveflow vendor files are not included in this repository. Users must obtain
them from Elveflow and install them locally. The COMPLX Python modules expect the
Elveflow Python wrapper (`Elveflow64.py`) and 64-bit DLL/runtime files to be
available locally before connecting to the OB1.

### 5. Set Up The DOBOT MG400

Install DOBOT Studio Pro and connect the MG400 over Ethernet:

https://www.dobot-robots.com/service/download-center

Configure the robot controller network settings, then load and run the included
`dobotserver.lua` script on the DOBOT before connecting from the COMPLX software.

Perform setup and calibration of the robotic arm as described in the manuscript
methods.

### 6. Set Up The Arduino Hardware

Install the Arduino IDE. In the Arduino IDE:

- Connect the Arduino controlling the collection stage.
- Select the correct board and COM port.
- Upload `arduino/arduino.ino`.
- If secondary valves are used, upload `arduino/arduino_secondary_valves.ino` to
  the secondary controller.

The collection-stage and valve controllers communicate with COMPLX over serial.
The manuscript workflow used serial communication at 115200 baud. If multiple
serial devices are attached, select the intended COM ports in the GUI or local
configuration before running automated collection.

### 7. Configure Local Settings

Hardware and app settings must be configured in the GUI before operation. The
GUI saves these selections and calibration values in `configs/` for reuse.

Key local settings include:

- Elveflow controller identifiers and optional extra pressure-controller
  settings.
- DOBOT IP address and port.
- Arduino COM ports for the collection stage and secondary valves.
- Sensor channel assignments and correction coefficients.
- Pressure limits, flow-control gains, timing parameters, and flush settings.
- Plate format, starting position, excluded wells, and plate calibration.

The saved config files will contain local serial numbers, COM ports, IP
addresses, calibration arrays, and run settings.

## Running COMPLX

From this folder, with the environment activated:

```powershell
python main.py
```

or, after editable installation:

```powershell
complx
```

The GUI creates runtime folders as needed and uses the configuration files in
`configs/` for hardware and run settings.

## Operating Workflow

The typical operating method follows this general sequence:

1. Start the hardware systems and launch COMPLX in the configured Conda
   environment by running main.py.
2. Connect to the pressure controller, DOBOT, collection stage, and valve
   microcontrollers through the software interface.
3. Select a user configuration defining timing, flow-control settings, sensor
   calibration, pressure limits, and active hardware.
4. Populate the digital lipid library with stock names, identifiers,
   concentrations, display colours, input-plate positions, and loading volumes.
5. Fill the corresponding input wells with the required lipid stock volume plus
   excess dead volume for robotic loading, then seal wells as required by the
   hardware workflow.
6. Record the aqueous phase or buffer and connect the buffer reservoir to the
   microfluidic manifold.
7. Create experiments in the GUI or import them from CSV. Each experiment defines
   lipid stocks, component ratios, total flow rate, flow-rate ratio, collection
   volume, repeats, and output wells.
8. Confirm output plate format, destination wells, excluded positions, and
   collection-plate placement.
9. Connect the microfluidic device to the lipid inlets, aqueous inlet, and
   particle outlet.
10. Inspect tubing, reservoirs, waste routing, robotic alignment, collection
   stage position, and plate seating.
11. Prime or flush lines, start the queue, and monitor live flow, pressure,
   robot, and collection status.
12. After completion, remove collection plates and run the final cleaning
   procedure.

## Outputs

During use the software creates local runtime and output folders:

- `logs/` - finalized JSON logs and run indexes.
- `FlowData/` - run folders, parameter records, flow traces, plots, and Excel
  logs.

For each formulation, the logging system records identifiers, lipid identities,
programmed compositions, assigned input and output wells, target and measured
flow rates, pressure set points, collection volume, equilibration and expulsion
timing, run events, status, and time-series flow data.

## Admin Tools

The GUI includes admin tools for setup, calibration, and troubleshooting. These
tools allow manual control of selected hardware functions, including DOBOT
movement, gripper and valve outputs, collection-stage movement, plate-position
calibration, sensor checks, priming, and cleaning routines.

Admin tools should be used only by trained operators during setup or fault
recovery. Confirm that pressure is safe, the collection stage is clear, and the
robot work envelope is unobstructed before using manual movement controls.

## First Run Checks

Before allowing pressure control or movement:

- Confirm tubing, reservoirs, waste routing, and chip connections are secure.
- Confirm pressure limits in `configs/app/config.json` are conservative for the
  chip and formulation.
- Confirm sensor channels and correction coefficients match the connected
  hardware.
- Confirm collection plate calibration and starting well are correct.
- Confirm the DOBOT, Arduino hardware, and Elveflow hardware can be stopped
  manually.
- Confirm destination wells are empty and the plate is correctly seated.
- Confirm the microfluidic outlet is directed to waste during priming and
  failure recovery.

## Safety Checklist

- Ensure a full local risk assessment is in place before operating the COMPLX
  platform.
- Verify tubing, reservoirs, waste routing, and chip connections before enabling
  pressure.
- Use conservative `p_range`, `p_incr`, and flow-rate values when testing new
  chips, sensors, or formulations.
- Keep hands clear of the robot and collection stage during automated motion.
- Confirm collection wells are empty before automated dispensing.
- Confirm pressure and robot emergency-stop procedures before unattended runs.
- Stop the run immediately if flow readings, pressure readings, or robot motion
  do not match the expected state.

## License

This software is released under the MIT License. See `LICENSE`.
