import threading
import queue
import time
import numpy as np
from typing import Optional, List, Dict, Tuple
import pump
import expel
from concurrent.futures import ThreadPoolExecutor
import ast
from elveflow_extra import ElveflowExtraPump

class MicrofluidicController:
    """Background microfluidic control loop with pause/resume/skip."""
    
    def __init__(self, status_broker, data_logger, lipid_tracker, on_composition_complete=None, on_experiment_complete=None):
        self.status_broker = status_broker
        self.data_logger = data_logger
        self.lipid_tracker = lipid_tracker
        self.on_composition_complete = on_composition_complete  # Callback(exp_id, comp_idx)
        self.on_experiment_complete = on_experiment_complete  # Callback(exp_id) when all compositions done
        
        # Command queue for GUI -> control loop
        self.command_queue: queue.Queue = queue.Queue()
        
        # State
        self.is_running = False
        self.is_paused = False
        self.should_skip = False
        self.control_thread: Optional[threading.Thread] = None
        self._stop_requested = False
        self._re_eq_required = False
        
        # Hardware handles (microfluidics only)
        self.ser = None
        self.ser_secondary = None
        self.calibarr = None
        
        # Control parameters
        self.active_channels = [1, 2, 3, 4]
        self.sensorcorr = [[0,0,0,1.0897,-1.2766],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2],[0.2673,-0.8813,1.3205,1.1869,-0.2]]
        self.period = 0.5
        self.K_p = np.array([0.5, 500, 500, 500])
        self.K_i = 0.001
        self.p_incr = [-100, 100]
        self.p_range = [0, 2000]
        self.flush_frs = [420, 4, 4, 4]
        self.start_flush1_frs = [420, 4, 4, 4]
        self.start_flush2_frs = [420, 4, 4, 4]
        self.start_flush = True
        self.zero_flush = False
        self.start_flush_ramp_enabled = True
        self.zero_flush_ramp_enabled = True
        self.zero_flow_blocking = True
        self.min_nonzero_set_fr = 0.25
        self.prime_buffer_fr = 100.0
        self.prime_lipid_fr = 20.0
        self.prime_rna_buffer_fr = 20.0
        self.rna_buffer_startflush_fr = 0.0
        self.rna_buffer_zeroflush_fr = 0.0
        
        # Current experiment state
        self.current_exp_id: Optional[str] = None
        self.current_composition_idx = 0
        self.collected_volume = 0.0
        self.target_volume = 100.0
        self.last_stable_pressures = [0, 0, 0, 0]
        self._start_well = (1, 1, 1)  # Add missing initialization
        self._current_well: Optional[Tuple[int, int, int]] = None
        self._manual_current_well: Optional[Tuple[int, int, int]] = None
        self._just_finished_priming = False  # Flag to keep servos in waste after priming
        self._last_primed_lines = set()
        self._last_prime_set_fr_full = None
        self.prime_to_startflush_ramp_s = 60.0
        self.flush_time_s = 0.0
        self.first_comp_delay_s = 0.0
        self.zero_block_hold_s = 3.0
        self.equilibration_retry = False
        self.maxfrerror = [100, 0.2]
        self.extra_pump = ElveflowExtraPump()
        self.extra_pump_connected = False
        self.extra_flow_last = None
        self.extra_pressure_last = 0.0
        self.extra_flow_kp = 3000.0
        self.extra_flow_ki = 0.001
        self.sensorcorr_extra_rna = [0.2673, -0.8813, 1.3205, 1.1869, -0.2]
        
        # Thread pool for non-blocking hardware movements
        self.hw_executor = ThreadPoolExecutor(max_workers=1)
        self.plate_calibration = {}
        self._config = {}  # Store current config

    def set_plate_calibration(self, plate_calibration: Dict) -> None:
        """Set per-plate calibration map {plate: {stepsH, stepsV}}."""
        self.plate_calibration = plate_calibration or {}

    def _get_plate_calibration(self, plate: int) -> Optional[Dict[str, float]]:
        plate_key = str(plate)
        return self.plate_calibration.get(plate_key) or self.plate_calibration.get(plate)

    def jog_stage(self, dir_h: str, dir_v: str, steps_h: int, steps_v: int) -> None:
        """Jog stage by steps (blocking)."""
        if not self.ser:
            raise RuntimeError("Microcontroller not connected - cannot move stage")
        expel.move(self.ser, dir_h, dir_v, steps_h, steps_v)

    def home_to_plate(self, plate: int) -> None:
        """Home the stage, then move to calibrated first well for a plate if defined."""
        if not self.ser:
            raise RuntimeError("Microcontroller not connected - cannot home stage")
        calib = self._get_plate_calibration(plate)
        if calib and "stepsH" in calib and "stepsV" in calib:
            expel.home(self.ser)
            expel.setstep(self.ser, calib["stepsH"], calib["stepsV"])
        else:
            expel.home(self.ser)

    def set_manual_current_well(self, well: Tuple[int, int, int]) -> None:
        """Track current well for manual moves and update status."""
        self._manual_current_well = well
        self._current_well = well
        self.status_broker.set_current_well(well[1], well[2], well[0])

    def move_to_well(self, target_well: Tuple[int, int, int], rehome: bool = False) -> Tuple[int, int, int]:
        """Move to a well for manual testing (optionally re-home first)."""
        if not self.ser:
            raise RuntimeError("Microcontroller not connected - cannot move stage")
        if rehome or self._manual_current_well is None:
            self._home_and_move_to_well(target_well)
        else:
            self._manual_current_well = self._move_to_well_sync(self._manual_current_well, target_well)
        self._manual_current_well = target_well
        self._current_well = target_well
        self.status_broker.set_current_well(target_well[1], target_well[2], target_well[0])
        return target_well

    def enter_calibration_mode(self, z_down_steps: int = int(1300 * 2 / 3)) -> None:
        """Switch to collect mode and lower Z for calibration."""
        if not self.ser:
            raise RuntimeError("Microcontroller not connected - cannot enter calibration")
        expel.servoswitch(self.ser, 1)
        expel.movez(self.ser, "Down", int(z_down_steps), 400)

    def exit_calibration_mode(self, z_up_steps: int = 1300) -> None:
        """Raise Z and return to waste mode after calibration."""
        if not self.ser:
            return
        expel.movez(self.ser, "Up", int(z_up_steps), 400)
        expel.servoswitch(self.ser, 0)

    def _home_and_move_to_well(self, target_well: Tuple[int, int, int]) -> None:
        plate, row, col = target_well
        calib = self._get_plate_calibration(plate)
        if calib and "stepsH" in calib and "stepsV" in calib:
            expel.home(self.ser)
            h_abs, v_abs = self._well_absolute_steps(target_well)
            expel.setstep(self.ser, h_abs, v_abs)
        else:
            expel.homeandfirst(self.ser, [1, 1], [row, col], "96")

    def _move_to_well_sync(self, current_well: Tuple[int, int, int], target_well: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Move from current well to target well using calibrated absolute-grid deltas."""
        if not self.ser:
            return target_well
        if current_well is None:
            return target_well
        if not self._move_between_wells_absolute(current_well, target_well, queued=False):
            if current_well[0] != target_well[0]:
                self._home_and_move_to_well(target_well)
            else:
                expel.nextwell(self.ser, [current_well[1], current_well[2]], [target_well[1], target_well[2]], "96")
        return target_well

    def _queue_move_to_well(self, current_well: Tuple[int, int, int], target_well: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Queue move from current well to target well using calibrated absolute-grid deltas."""
        if not self.ser:
            return target_well
        if current_well is None:
            return target_well
        if not self._move_between_wells_absolute(current_well, target_well, queued=True):
            if current_well[0] != target_well[0]:
                self.hw_executor.submit(lambda tw=target_well: self._home_and_move_to_well(tw))
            else:
                self.hw_executor.submit(
                    lambda cw=current_well, tw=target_well: expel.nextwell(self.ser, [cw[1], cw[2]], [tw[1], tw[2]], "96")
                )
        return target_well

    def _get_plate_origin_steps(self, plate: int) -> Optional[Tuple[float, float]]:
        calib = self._get_plate_calibration(plate)
        if not calib or "stepsH" not in calib or "stepsV" not in calib:
            return None
        return float(calib["stepsH"]), float(calib["stepsV"])

    def _well_offset_steps(self, row: int, col: int) -> Tuple[float, float]:
        # 96-well step sizes matching expel.nextwell
        v_per_row = (1675 / 2) / 7
        h_per_col = (2637.5 / 2) / 11
        vstep = v_per_row * (row - 1)
        hstep = h_per_col * (col - 1)
        return hstep, vstep

    def _well_absolute_steps(self, well: Tuple[int, int, int]) -> Tuple[int, int]:
        plate, row, col = well
        origin = self._get_plate_origin_steps(plate)
        if origin is None:
            raise RuntimeError(f"No plate calibration available for plate {plate}")
        h_off, v_off = self._well_offset_steps(row, col)
        return int(round(origin[0] + h_off)), int(round(origin[1] + v_off))

    def _move_between_wells_absolute(
        self,
        current_well: Tuple[int, int, int],
        target_well: Tuple[int, int, int],
        *,
        queued: bool,
    ) -> bool:
        """Move by the difference between rounded absolute well coordinates."""
        try:
            cur_h, cur_v = self._well_absolute_steps(current_well)
            tgt_h, tgt_v = self._well_absolute_steps(target_well)
        except Exception:
            return False

        delta_h = tgt_h - cur_h
        delta_v = tgt_v - cur_v
        steps_h = abs(int(delta_h))
        steps_v = abs(int(delta_v))
        if steps_h == 0 and steps_v == 0:
            return True
        dir_h = "Away" if delta_h > 0 else "Towards"
        dir_v = "Away" if delta_v > 0 else "Towards"
        print(
            f"[MicrofluidicController] Well move absolute-grid: "
            f"P{current_well[0]} {current_well[1]},{current_well[2]} -> "
            f"P{target_well[0]} {target_well[1]},{target_well[2]} "
            f"(H {cur_h}->{tgt_h}, V {cur_v}->{tgt_v}, steps H={steps_h}, V={steps_v})"
        )
        if queued:
            self.hw_executor.submit(lambda: expel.move(self.ser, dir_h, dir_v, steps_h, steps_v))
        else:
            expel.move(self.ser, dir_h, dir_v, steps_h, steps_v)
        return True

    def _normalize_active_channels(self, active_channels: Optional[List[int]]) -> List[int]:
        if not active_channels:
            return [1, 2, 3, 4]
        normalized: List[int] = []
        for ch in active_channels:
            try:
                ch_int = int(ch)
            except Exception:
                continue
            if 1 <= ch_int <= 4 and ch_int not in normalized:
                normalized.append(ch_int)
        if 1 not in normalized:
            normalized.insert(0, 1)
        else:
            normalized = [1] + [ch for ch in normalized if ch != 1]
        return normalized

    def _inactive_channels(self, active_channels: List[int]) -> List[int]:
        return [ch for ch in (1, 2, 3, 4) if ch not in active_channels]

    def _set_inactive_pressures_zero(self, inactive_channels: List[int]) -> None:
        if not self.calibarr:
            return
        for ch in inactive_channels:
            try:
                pump.set_pressure(ch, 0, self.calibarr)
            except Exception:
                pass

    def _set_unused_line_servos(self, active_lines: List[int]) -> None:
        if not self.ser:
            return
        active_set = set(active_lines)
        for line in (1, 2, 3):
            if line in active_set:
                continue
            try:
                expel.set_servo_angle(self.ser, line, 40)  # Close to dobot
                print(f"[Servo] Close to chip: servo {line + 3} (line {line}) reason=inactive_line")
                expel.set_servo_angle(self.ser, line + 3, 40)  # Close to chip
            except Exception:
                pass

    def _set_active_line_servos_to_waste(self, active_lines: List[int], reason: str = "experiment_start") -> None:
        """Put active lipid lines in the pressure-driven waste path before flow control."""
        if not self.ser:
            return
        active_set = sorted({int(line) for line in (active_lines or []) if int(line) in (1, 2, 3)})
        if not active_set:
            return
        for line in active_set:
            try:
                expel.set_servo_angle(self.ser, line, 40)  # Close to dobot
                expel.set_servo_angle(self.ser, line + 3, 125)  # Close to waste
                print(
                    f"[Servo] Active line to waste: line {line}, "
                    f"servos {line}=40 and {line + 3}=125 reason={reason}"
                )
            except Exception as e:
                print(f"[Servo] Warning: could not set line {line} to waste ({reason}): {e}")
        print("[MicrofluidicController] Waiting 0.5s for active-line servos to settle")
        time.sleep(0.5)

    def _map_active_values_to_full(self, active_channels: List[int], values: List[float]) -> List[float]:
        full = [0.0, 0.0, 0.0, 0.0]
        for idx, ch in enumerate(active_channels):
            if idx < len(values):
                full[ch - 1] = values[idx]
        return full

    def _filter_flow_rate_for_active_channels(self, fr: List[float], active_channels: List[int]) -> List[float]:
        return [fr[ch - 1] if (ch - 1) < len(fr) else 0.0 for ch in active_channels]

    def _connect_extra_pump(
        self,
        com_port: str = "",
        device_name: str = "Mk4_Extra",
        calibration: str = "load",
    ) -> None:
        ok, err = self.extra_pump.connect(
            com_port=com_port or "COM6",
            device_name=device_name or "Mk4_Extra",
            calibration=calibration,
        )
        self.extra_pump_connected = bool(ok)
        self.status_broker.set_connection_state("extra_pressure", bool(ok))
        if not ok:
            raise RuntimeError(err or "Failed to connect extra Mk4 pump")
        print("[MicrofluidicController] Extra Mk4 pump connected")

    def _set_extra_pressure_zero(self) -> None:
        if not self.extra_pump_connected:
            self.extra_flow_last = None
            self.extra_pressure_last = 0.0
            return
        try:
            self.extra_pump.set_pressure(0.0)
        except Exception:
            pass
        self.extra_pressure_last = 0.0

    def _read_extra_flow(self) -> Optional[float]:
        if not self.extra_pump_connected:
            self.extra_flow_last = None
            return None
        val, _ = self.extra_pump.get_flow()
        if val is not None:
            self.extra_flow_last = float(val)
        return self.extra_flow_last

    def _correct_extra_rna_flow(self, fr_raw: float) -> float:
        coeff = list(self.sensorcorr_extra_rna or [0, 0, 0, 1, 0])
        while len(coeff) < 5:
            coeff.append(0.0)
        fr = float(fr_raw)
        corrected = (
            float(coeff[0]) * (fr ** 4)
            + float(coeff[1]) * (fr ** 3)
            + float(coeff[2]) * (fr ** 2)
            + float(coeff[3]) * fr
            + float(coeff[4])
        )
        return corrected if fr > 0 else -abs(corrected)

    def initialize(
        self,
        sensor_config: List,
        calibration: str = "load",
        device_id: str = "",
        connect_extra_pressure: bool = False,
        extra_pressure_com_port: str = "",
    ):
        """Initialize pumps and sensors only (no COM)."""
        try:
            # Pumps
            error = pump.pressure_init(device_id=device_id)
            if error != 0:
                raise RuntimeError(f"OB1_Initialization error: {error}")

            cal = str(calibration).strip().lower()
            if cal not in ("load", "new", "default"):
                cal = "load"
            self.calibarr, calib_err = pump.pressure_calib(cal)
            if calib_err != 0:
                raise RuntimeError(f"Calibration error: {calib_err}")

            # Sensors
            sensor_err = pump.sensor_init(*sensor_config)
            if sensor_err != 0:
                raise RuntimeError(f"Sensor init error: {sensor_err}")

            # Log current flow rates to verify sensors are reading
            import time
            time.sleep(0.5)  # Give sensors time to stabilize
            print("\n[Sensor Check] Current raw flow rates:")
            for ch in range(1, 5):
                fr_raw, err = pump.get_sensor_data(ch)
                status = "OK" if err == 0 else f"ERROR({err})"
                print(f"  Channel {ch}: raw={fr_raw:.6f} [{status}]")
            print()

            self.status_broker.set_connection_state("microfluidics", True)
            if connect_extra_pressure:
                self._connect_extra_pump(str(extra_pressure_com_port or ""))
            else:
                self.extra_pump_connected = False
                self.status_broker.set_connection_state("extra_pressure", False)
                self._set_extra_pressure_zero()
        except Exception as e:
            self.status_broker.set_error(f"Microfluidic init failed: {e}")
            raise

    def read_all_sensor_flows(self):
        """Read all sensors and print raw + corrected flow rates."""
        rows = self.get_sensor_snapshot()
        print("\n[Sensor Diagnostic] Raw and corrected flow rates:")
        for row in rows:
            ch = row["channel"]
            fr_raw = row["raw"]
            corrected = row["corrected"]
            err = row["error"]
            status = "OK" if err == 0 else f"ERROR({err})"
            if corrected is None:
                print(f"  Ch{ch}: raw={fr_raw:.6f}, corrected=NA [{status}]")
            else:
                print(f"  Ch{ch}: raw={fr_raw:.6f}, corrected={corrected:.6f} [{status}]")
        print()
        return rows

    def get_sensor_snapshot(self):
        """Return raw + corrected flow readings for channels 1..4 without printing."""
        rows = []
        for ch in range(1, 5):
            fr_raw, err = pump.get_sensor_data(ch)
            corrected = None
            if err == 0:
                if ch == 1:
                    corrected = (
                        self.sensorcorr[ch - 1][0] * (fr_raw ** 4)
                        + self.sensorcorr[ch - 1][1] * (fr_raw ** 3)
                        + self.sensorcorr[ch - 1][2] * (fr_raw ** 2)
                        + self.sensorcorr[ch - 1][3] * fr_raw
                        + self.sensorcorr[ch - 1][4]
                    )
                elif fr_raw > 0:
                    corrected = (
                        self.sensorcorr[ch - 1][0] * (fr_raw ** 4)
                        + self.sensorcorr[ch - 1][1] * (fr_raw ** 3)
                        + self.sensorcorr[ch - 1][2] * (fr_raw ** 2)
                        + self.sensorcorr[ch - 1][3] * fr_raw
                        + self.sensorcorr[ch - 1][4]
                    )
                else:
                    corrected = -abs(
                        self.sensorcorr[ch - 1][0] * (fr_raw ** 4)
                        + self.sensorcorr[ch - 1][1] * (fr_raw ** 3)
                        + self.sensorcorr[ch - 1][2] * (fr_raw ** 2)
                        + self.sensorcorr[ch - 1][3] * fr_raw
                        + self.sensorcorr[ch - 1][4]
                    )
            rows.append({"channel": ch, "raw": fr_raw, "corrected": corrected, "error": err})
        return rows

    def init_microcontroller(self, port_name, *, keep_servo_state: bool = False):
        """Initialize microcontroller COM and servos."""
        try:
            self.ser = expel.serconnect(port_name)
            if not keep_servo_state:
                # Default on connect:
                # - Servos 1-3 closed to dobot (40)
                # - Chip-side servos 4-6 neutral/open (80)
                for i in (1, 2, 3):
                    expel.set_servo_angle(self.ser, i, 40)
                for i in (4, 5, 6):
                    expel.set_servo_angle(self.ser, i, 80)
                expel.servoswitch(self.ser, 0)
            self.status_broker.set_connection_state("microcontroller", True)
        except Exception as e:
            self.status_broker.set_error(f"Microcontroller init failed: {e}")
            raise

    def init_secondary_microcontroller(self, port_name: str, *, keep_servo_state: bool = False):
        """Initialize optional secondary microcontroller COM."""
        try:
            self.ser_secondary = expel.serconnect(port_name)
            # Give the board time to reset after opening serial.
            time.sleep(1.5)
            if not keep_servo_state:
                for servo_number in (7, 8, 9):
                    expel.set_servo_angle(self.ser_secondary, servo_number, 80)
                    time.sleep(0.2)
        except Exception as e:
            raise RuntimeError(f"Secondary microcontroller init failed: {e}")

    def start(self):
        """Start background control loop."""
        if self.is_running:
            return
        self.is_running = True
        self.control_thread = threading.Thread(target=self._run, daemon=True)
        self.control_thread.start()

    def home_to_start(self, start_well: Tuple):
        """Home the stage and move to the first collection well."""
        if not self.ser:
            raise RuntimeError("Microcontroller not connected - cannot home stage")
        
        plate, row, col = start_well
        print(f"[MicrofluidicController] Homing stage and moving to well (P{plate} {row},{col})")
        calib = self._get_plate_calibration(plate)
        if calib and "stepsH" in calib and "stepsV" in calib:
            expel.home(self.ser)
            h_abs, v_abs = self._well_absolute_steps(start_well)
            expel.setstep(self.ser, h_abs, v_abs)
        else:
            expel.homeandfirst(self.ser, [1, 1], [row, col], "96")
        
        # Update internal state
        self._start_well = start_well
        self._current_well = start_well

    def stop(self):
        """Stop background loop."""
        self.is_running = False
        if self.control_thread:
            self.control_thread.join(timeout=5)

    def prime_lines(
        self,
        loaded_lines: List[int],
        line3_constant_active: bool = False,
        extra_rna_active: bool = False,
    ):
        """Prime loaded lipid lines: drive 20µL at 20µL/min, buffer at 50µL/min.
        
        Args:
            loaded_lines: List of line indices that were loaded (1-3)
        """
        print(f"[MicrofluidicController] prime_lines() called with {loaded_lines}")
        # Ensure background thread is running
        if not self.is_running:
            print("[MicrofluidicController] Starting background thread...")
            self.start()
        self.command_queue.put({
            "cmd": "prime_lines",
            "loaded_lines": loaded_lines,
            "line3_constant_active": bool(line3_constant_active),
            "extra_rna_active": bool(extra_rna_active),
        })
        print(f"[MicrofluidicController] prime_lines command queued")

    def queue_experiment(self, exp_id: str, flow_rates: List[List[float]], params: Dict):
        """Queue experiment for execution."""
        self.command_queue.put({
            "cmd": "run_experiment",
            "exp_id": exp_id,
            "flow_rates": flow_rates,
            "params": params
        })

    def pause(self):
        """Pause current experiment."""
        self.is_paused = True
        self._re_eq_required = True
        self.status_broker.set_paused(True)

    def resume(self):
        """Resume paused experiment."""
        self.is_paused = False
        self.status_broker.set_paused(False)

    def stop_experiment(self):
        """Stop and reset experiment."""
        self._stop_requested = True
        self.command_queue.put({"cmd": "stop"})

    def _run(self):
        """Main control loop (background thread)."""
        print("[MicrofluidicController] Background thread started")
        while self.is_running:
            try:
                cmd = self.command_queue.get(timeout=0.5)
                print(f"[MicrofluidicController] Got command from queue: {cmd}")
                self._handle_command(cmd)
            except queue.Empty:
                pass  # Normal, no command available
            except Exception as e:
                print(f"[MicrofluidicController] Loop error: {e}")
                time.sleep(0.1)

    def _handle_command(self, cmd: Dict):
        """Process queued command."""
        try:
            print(f"[MicrofluidicController] _handle_command: {cmd}")
            if cmd["cmd"] == "run_experiment":
                self._run_experiment(cmd["exp_id"], cmd["flow_rates"], cmd["params"])
            elif cmd["cmd"] == "prime_lines":
                print(f"[MicrofluidicController] Executing prime_lines with {cmd['loaded_lines']}")
                self._prime_lines_impl(
                    cmd["loaded_lines"],
                    bool(cmd.get("line3_constant_active", False)),
                    bool(cmd.get("extra_rna_active", False)),
                )
            elif cmd["cmd"] == "stop":
                self._stop_flows()
                self._end_collection_to_waste()
        except Exception as e:
            print(f"[MicrofluidicController] Command error: {e}")
            self._enter_fail_safe(f"Command failed: {e}")

    def _prime_lines_impl(
        self,
        loaded_lines: List[int],
        line3_constant_active: bool = False,
        extra_rna_active: bool = False,
    ):
        """Prime loaded lipid lines: run flush composition [100, 20, 20, 20] until equilibrium + 120s hold."""
        try:
            import expel
            import time
            
            print(f"[MicrofluidicController] Starting prime flush for lines {loaded_lines}")
            line3_main_pump_rna = bool(line3_constant_active and not self.extra_pump_connected)
            extra_rna_prime = bool(extra_rna_active and self.extra_pump_connected)

            active_lines = sorted({int(l) for l in loaded_lines if int(l) in (1, 2, 3)})
            if not active_lines:
                print("[MicrofluidicController] No loaded lines provided for priming - skipping")
                return

            active_channels = self._normalize_active_channels([1] + [line + 1 for line in active_lines])
            self.active_channels = active_channels
            inactive_channels = self._inactive_channels(active_channels)
            self._set_inactive_pressures_zero(inactive_channels)
            self._set_unused_line_servos(active_lines)
            
            # Set all servo valves to waste for priming
            if self.ser:
                future = self.hw_executor.submit(
                    self._set_active_line_servos_to_waste,
                    active_lines,
                    "prime_start",
                )
                future.result()  # Wait for servo setup to complete
                print("[MicrofluidicController] Waiting 3s for prime servo valves to actuate")
                time.sleep(3.0)  # Wait for servo valves to physically close
            
            # Ensure all dobot valves are OFF
            print("[MicrofluidicController] Setting dobot valves OFF")
            for i in range(1, 5):
                result = pump.set_pressure(i, 0, self.calibarr)
                print(f"[MicrofluidicController] Channel {i} valve OFF: {result}")
            
            time.sleep(0.5)  # Brief settle time
            
            # Use _run_experiment with prime flush composition.
            # IMPORTANT: provide full 4-channel FRs here; _run_experiment will
            # filter by active_channels. A compact list like [100, 20] breaks
            # for non-consecutive active channels (e.g., [1, 3]) and maps to 0.
            prime_fr_full = [float(self.prime_buffer_fr), 0.0, 0.0, 0.0]  # [buffer, L1, L2, L3]
            for line in active_lines:
                ch = line + 1  # line 1..3 -> channel 2..4
                if 1 <= ch <= 4:
                    if int(line) == 3 and line3_main_pump_rna:
                        prime_fr_full[ch - 1] = float(self.prime_rna_buffer_fr)
                    else:
                        prime_fr_full[ch - 1] = float(self.prime_lipid_fr)
            prime_flow_rates = [prime_fr_full]  # Single composition
            self._last_prime_set_fr_full = list(prime_fr_full)
            print(
                "[MicrofluidicController] Prime setpoints [buffer,L1,L2,L3]="
                f"{prime_fr_full} "
                f"(line3_main_pump_rna={line3_main_pump_rna}, extra_rna_prime={extra_rna_prime})"
            )
            
            # Get priming hold time from config (default 30s)
            priming_hold_time = float(self._config.get("priming_hold_time", 30.0))
            
            prime_params = {
                "volume": 0.0,  # No collection volume target (use collect_duration instead)
                "eq_max_t": 600.0,  # 10 min max
                "tubingdim": [0.51, 750],
                "maxfrerror": list(self.maxfrerror),
                "start_comp_idx": 0,
                "autocollect": False,  # Don't collect to chip
                "expul_t": 2.0,  # Wait 2s to equilibrate before holding
                "hold_after_eq": priming_hold_time,  # Hold time after reaching equilibrium (configurable)
                "collect_duration": priming_hold_time,  # Run collection loop for this duration
                "force_collect": True,  # Treat as non-flush even with volume=0
                "skip_collect_move": True,  # Do not lower Z or switch to collect mode
                "active_channels": active_channels,
                "active_lines": active_lines,
                "line3_constant_flow_enabled": bool(line3_main_pump_rna or extra_rna_prime),
                "line3_constant_flow_rate": float(
                    self.prime_rna_buffer_fr if (line3_main_pump_rna or extra_rna_prime) else 0.0
                ),
            }
            
            print(f"[MicrofluidicController] Starting prime experiment (hold_time={priming_hold_time}s)")
            self._run_experiment("prime_flush", prime_flow_rates, prime_params)
            
            # Flag that we just finished priming - keep servos in waste during transition
            self._just_finished_priming = True
            self._last_primed_lines = set(active_lines)
            print("[MicrofluidicController] Prime flush finished successfully")
            
        except Exception as e:
            import traceback
            print(f"[MicrofluidicController] Prime flush failed: {e}")
            traceback.print_exc()
            self._enter_fail_safe(f"Prime flush failed: {e}")
            raise

    def _run_experiment(self, exp_id: str, flow_rates: List[List[float]], params: Dict):
        """Run single experiment with PI control loop."""
        try:
            self.current_exp_id = exp_id
            self._stop_requested = False
            self.data_logger.init_experiment_log(exp_id, exp_params=params)
            self.status_broker.set_current_experiment(exp_id)
            self.status_broker.clear_flow_data()
            # Set initial state to indicate we're running
            self.status_broker.set_microfluidic_state("Running")
            
            # If we just finished priming, keep servos in waste until actual experiment starts
            is_priming_run = (exp_id == "prime_flush")
            just_transitioned_from_priming = self._just_finished_priming and not is_priming_run
            if just_transitioned_from_priming:
                self._just_finished_priming = False
                print("[MicrofluidicController] Transitioning from priming to experiment - keeping servos in waste")
            
            autocollect = bool(params.get("autocollect", True))
            start_comp_idx = int(params.get("start_comp_idx", 0))
            hold_after_eq = float(params.get("hold_after_eq", 0.0))  # Hold time after equilibration (for priming)
            collect_duration = params.get("collect_duration", None)
            force_collect = bool(params.get("force_collect", False))
            skip_collect_move = bool(params.get("skip_collect_move", False))
            extra_flow_enabled = False
            extra_flow_setpoint = 0.0

            active_channels = self._normalize_active_channels(params.get("active_channels"))
            self.active_channels = active_channels
            channel_to_index = {ch: idx for idx, ch in enumerate(self.active_channels)}
            inactive_channels = self._inactive_channels(self.active_channels)
            self._set_inactive_pressures_zero(inactive_channels)

            active_lines = params.get("active_lines")
            active_lines_set = set(active_lines or [])
            line3_constant_flow_enabled = bool(params.get("line3_constant_flow_enabled", False))
            line3_constant_flow_rate = float(params.get("line3_constant_flow_rate", 0.0) or 0.0)
            if line3_constant_flow_rate <= 0:
                line3_constant_flow_enabled = False
            rna_on_extra_controller = bool(line3_constant_flow_enabled and self.extra_pump_connected)
            if line3_constant_flow_enabled and not rna_on_extra_controller:
                active_lines_set.add(3)
            rna_run_fr = float(line3_constant_flow_rate if line3_constant_flow_enabled else 0.0)
            rna_startflush_fr = float(self.rna_buffer_startflush_fr if self.rna_buffer_startflush_fr > 0 else rna_run_fr)
            rna_zeroflush_fr = float(self.rna_buffer_zeroflush_fr if self.rna_buffer_zeroflush_fr > 0 else rna_run_fr)

            active_flush_frs = self._filter_flow_rate_for_active_channels(self.flush_frs, self.active_channels)
            active_start_flush1_frs = self._filter_flow_rate_for_active_channels(self.start_flush1_frs, self.active_channels)
            active_start_flush2_frs = self._filter_flow_rate_for_active_channels(self.start_flush2_frs, self.active_channels)
            if line3_constant_flow_enabled and not rna_on_extra_controller and 4 in self.active_channels:
                idx_ch4 = self.active_channels.index(4)
                if idx_ch4 < len(active_flush_frs):
                    active_flush_frs[idx_ch4] = rna_zeroflush_fr
                if idx_ch4 < len(active_start_flush1_frs):
                    active_start_flush1_frs[idx_ch4] = rna_startflush_fr
                if idx_ch4 < len(active_start_flush2_frs):
                    active_start_flush2_frs[idx_ch4] = rna_startflush_fr
            
            # Insert flush compositions based on config
            modified_flow_rates = [
                self._filter_flow_rate_for_active_channels(fr, self.active_channels)
                for fr in flow_rates
            ]
            if line3_constant_flow_enabled and not rna_on_extra_controller and 4 in self.active_channels:
                idx_ch4 = self.active_channels.index(4)
                for fr in modified_flow_rates:
                    if idx_ch4 < len(fr):
                        fr[idx_ch4] = rna_run_fr
            flush_kind = [None] * len(flow_rates)  # modified index -> "start" | "zero" | None
            start_flush2_targets = [None] * len(flow_rates)  # modified index -> FR target list | None
            # Map modified indices back to original composition indices (None for flush)
            comp_idx_map = list(range(len(flow_rates)))
            
            # Skip flush insertion for prime_flush (it's already a flush composition)
            is_priming = (exp_id == "prime_flush")
            
            # StartFlush: Insert flush at the beginning
            if self.start_flush and start_comp_idx == 0 and not is_priming:
                modified_flow_rates.insert(0, active_start_flush1_frs)
                comp_idx_map.insert(0, None)
                flush_kind.insert(0, "start")
                start_flush2_targets.insert(0, active_start_flush2_frs)
                print("[MicrofluidicController] StartFlush enabled - inserted flush at start")
            
            # ZeroFlush: Insert flush after compositions with zero flow that precede non-zero flow
            if self.zero_flush and not is_priming:
                insertions = []
                for j in range(len(modified_flow_rates) - 1):
                    current_fr = modified_flow_rates[j]
                    next_fr = modified_flow_rates[j + 1]
                    # Check if any lipid channel goes from ~0 to >0
                    for ch_idx in range(1, len(current_fr)):
                        if current_fr[ch_idx] < 0.01 and next_fr[ch_idx] > 0.01:
                            insertions.append(j + 1)
                            break
                # Insert flushes (in reverse to maintain indices)
                for insert_idx in reversed(insertions):
                    # Zero flush FR source depends on zero-flush ramp setting:
                    # - ramp enabled: start at StartFlush1 FRs and ramp to FR2
                    # - ramp disabled: use legacy FlushFRs directly
                    zero_flush_insert_frs = (
                        active_start_flush1_frs if self.zero_flush_ramp_enabled else active_flush_frs
                    )
                    if line3_constant_flow_enabled and not rna_on_extra_controller and 4 in self.active_channels:
                        idx_ch4 = self.active_channels.index(4)
                        if idx_ch4 < len(zero_flush_insert_frs):
                            zero_flush_insert_frs = list(zero_flush_insert_frs)
                            zero_flush_insert_frs[idx_ch4] = rna_zeroflush_fr
                    modified_flow_rates.insert(insert_idx, zero_flush_insert_frs)
                    comp_idx_map.insert(insert_idx, None)
                    flush_kind.insert(insert_idx, "zero")
                    start_flush2_targets.insert(insert_idx, None)
                    print(f"[MicrofluidicController] ZeroFlush: inserted flush at position {insert_idx}")
            
            flow_rates = modified_flow_rates
            skip_indices = {i for i, v in enumerate(comp_idx_map) if v is None}

            # If active_lines not provided, infer from any non-zero lipid flow across compositions
            if not active_lines:
                inferred_lines = set()
                for fr in flow_rates:
                    for i, channel in enumerate(self.active_channels):
                        if channel == 1:
                            continue
                        if i < len(fr) and fr[i] > 0:
                            inferred_lines.add(channel - 1)
                active_lines = sorted(inferred_lines)
            active_lines_set = set(active_lines or [])
            if line3_constant_flow_enabled and not rna_on_extra_controller:
                active_lines_set.add(3)
                active_lines = sorted(active_lines_set)

            self._set_unused_line_servos(active_lines)
            self._set_active_line_servos_to_waste(active_lines, "experiment_start")
            
            # Collection well tracking
            output_wells = params.get("output_wells", []) or []
            output_wells = [tuple(w) for w in output_wells]
            wpdim = [8, 12]
            current_well = self._current_well or self._start_well

            # Debug: print composition plan (including flush steps)
            try:
                print("[MicrofluidicController] Composition plan (including flush steps):")
                for idx, fr in enumerate(flow_rates):
                    orig_idx_dbg = comp_idx_map[idx] if idx < len(comp_idx_map) else idx
                    is_flush_dbg = idx in skip_indices or (self.target_volume == 0 and not force_collect)
                    if orig_idx_dbg is None:
                        well_dbg = "-"
                    else:
                        if output_wells and orig_idx_dbg < len(output_wells):
                            w = output_wells[orig_idx_dbg]
                            well_dbg = f"P{w[0]} {w[1]},{w[2]}"
                        else:
                            well_dbg = "-"
                    action = "SKIP" if is_flush_dbg else "COLLECT"
                    print(f"  idx={idx:02d} orig={orig_idx_dbg} {action} well={well_dbg} fr={fr}")
            except Exception as e:
                print(f"[MicrofluidicController] Could not print composition plan: {e}")

            if output_wells and start_comp_idx < len(output_wells):
                target_start = output_wells[start_comp_idx]
                if autocollect and self.ser and target_start != current_well:
                    current_well = self._move_to_well_sync(current_well, target_start)
                else:
                    current_well = target_start
                wpcurrent = [current_well[1], current_well[2]]
            else:
                wpcurrent = [self._start_well[1], self._start_well[2]]  # Extract row and col from (plate, row, col)
                # Advance to the correct well if resuming mid-experiment
                if start_comp_idx > 0:
                    for _ in range(start_comp_idx):
                        wpcurrent = self._advance_well(wpcurrent, wpdim)
            
            # Update status broker with current well position
            self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
            self._current_well = current_well

            # Initialize PI control state ONCE before all compositions (not per composition)
            # This allows pressures to carry forward between compositions smoothly
            pr_control = np.array([0.0] * len(self.active_channels))
            I = np.array([0.0] * len(self.active_channels))
            error_prev = np.array([0.0] * len(self.active_channels))
            # Track pressure history for linear interpolation (per channel)
            pr_history = [[] for _ in range(len(self.active_channels))]
            if just_transitioned_from_priming:
                # Preserve pressure continuity from prime flush into first experiment.
                for i, ch in enumerate(self.active_channels):
                    try:
                        p_val, p_err = pump.get_pressure_data(ch, self.calibarr)
                        if p_err == 0 and p_val is not None:
                            pr_control[i] = float(np.clip(p_val, self.p_range[0], self.p_range[1]))
                    except Exception:
                        pass
                    pr_history[i].append(float(pr_control[i]))
                print(
                    "[MicrofluidicController] Priming->run pressure continuity seed: "
                    + ", ".join(f"ch{ch}={pr_control[i]:.1f}" for i, ch in enumerate(self.active_channels))
                )
            # Recorded once per experiment: equilibrium pressures at first start flush.
            start_flush_equilibrium_pressures = None
            
            init_start_t = time.time()
            extra_i_term = 0.0
            extra_error_prev = 0.0
            extra_pmin = float(getattr(self.extra_pump, "pressure_min", 0.0) or 0.0)
            extra_pmax = float(getattr(self.extra_pump, "pressure_max", 1000.0) or 1000.0)
            extra_pmax_safe = max(extra_pmin, extra_pmax - 1.0) if extra_pmax > extra_pmin else extra_pmax
            extra_pressure = float(np.clip(self.extra_pressure_last, extra_pmin, extra_pmax_safe))
            extra_pr_history = [float(extra_pressure)]
            if not rna_on_extra_controller:
                self._set_extra_pressure_zero()
            
            # Get composition status to skip already completed ones
            comp_status = params.get("comp_status", [])
            # Track original compositions that have already used one equilibration retry.
            eq_retry_used = set()
            
            for comp_idx, set_FR in enumerate(flow_rates):
                # Replace exact-zero setpoints with a small non-zero floor.
                # This avoids commanding true zero flow during PI control.
                set_FR = [
                    (float(self.min_nonzero_set_fr) if float(fr) == 0.0 else float(fr))
                    for fr in set_FR
                ]
                orig_idx = comp_idx_map[comp_idx] if comp_idx < len(comp_idx_map) else comp_idx
                if orig_idx is not None and orig_idx < start_comp_idx:
                    continue
                
                # Skip if this composition is already marked as completed (original index)
                if orig_idx is not None and orig_idx < len(comp_status) and comp_status[orig_idx] == "completed":
                    print(f"[MicrofluidicController] Skipping composition {orig_idx} - already completed")
                    if autocollect and self.ser:
                        if output_wells and orig_idx + 1 < len(output_wells):
                            next_target = output_wells[orig_idx + 1]
                            current_well = self._queue_move_to_well(current_well, next_target)
                            wpcurrent = [current_well[1], current_well[2]]
                            self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
                            self._current_well = current_well
                        else:
                            wpcurrent = self._advance_well(wpcurrent, wpdim)
                            self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
                    continue
                
                if not self.is_running:
                    break
                
                self.current_composition_idx = orig_idx if orig_idx is not None else comp_idx
                self.status_broker.set_current_experiment(exp_id, self.current_composition_idx)
                # Ensure stage is at the correct well before starting a real composition
                if autocollect and self.ser and orig_idx is not None and output_wells and orig_idx < len(output_wells):
                    target_well = output_wells[orig_idx]
                    if current_well != target_well:
                        current_well = self._move_to_well_sync(current_well, target_well)
                        wpcurrent = [current_well[1], current_well[2]]
                        self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
                        self._current_well = current_well
                # Update plot ranges based on set flow rates and pressure range
                try:
                    lipid_plot_flows = []
                    for i, ch in enumerate(self.active_channels):
                        if int(ch) == 1:
                            continue  # buffer channel
                        if line3_constant_flow_enabled and not rna_on_extra_controller and int(ch) == 4:
                            continue  # exclude RNA buffer line from lipid plot autoscale
                        if i < len(set_FR):
                            lipid_plot_flows.append(float(set_FR[i]))
                    flow_max = float(np.amax(lipid_plot_flows)) * 1.5 if lipid_plot_flows else 1.0
                except Exception:
                    flow_max = 1.0
                self.status_broker.set_plot_ranges([-1, flow_max], [self.p_range[0], self.p_range[1]])
                
                # Reset state for this composition
                collection = False
                self.collected_volume = 0.0
                self.target_volume = params.get("volume", 100)
                self.status_broker.set_collection_progress(0, self.target_volume)
                
                # Calculate expected collection time based on SET flow rates
                total_set_flow = np.sum(set_FR)  # Total flow in µL/min
                if collect_duration is not None:
                    expected_collection_time = float(collect_duration)
                else:
                    expected_collection_time = (self.target_volume / total_set_flow) * 60.0 if total_set_flow > 0 else 180.0  # seconds
                collection_start_time = None  # Will be set when collection starts
                print(f"[MicrofluidicController] Expected collection time: {expected_collection_time:.1f}s at {total_set_flow:.1f}µL/min total flow")

                # Determine flush type early for per-composition setup decisions.
                # Do not rely on the original skip_indices only: retry flushes are inserted
                # dynamically after the plan is built, so treat any step with no original
                # composition index or an explicit flush kind as a flush step.
                is_flush_step = (
                    orig_idx is None
                    or (comp_idx < len(flush_kind) and flush_kind[comp_idx] is not None)
                )
                flush_type_current = flush_kind[comp_idx] if comp_idx < len(flush_kind) else None
                extra_rna_set_fr = 0.0
                if rna_on_extra_controller:
                    if flush_type_current == "zero":
                        extra_rna_set_fr = rna_zeroflush_fr
                    elif flush_type_current == "start":
                        extra_rna_set_fr = rna_startflush_fr
                    else:
                        extra_rna_set_fr = rna_run_fr
                    if extra_rna_set_fr == 0.0:
                        extra_rna_set_fr = float(self.min_nonzero_set_fr)
                    extra_flow_enabled = True
                    extra_flow_setpoint = float(extra_rna_set_fr)
                # Flush ramp target 2 comes from active StartFlush2 FRs and is used
                # for both start-flush and zero-flush compositions.
                start_flush2_target = (
                    list(active_start_flush2_frs) if flush_type_current in ("start", "zero") else None
                )
                start_flush2_target = (
                    [
                        (float(self.min_nonzero_set_fr) if float(fr) == 0.0 else float(fr))
                        for fr in start_flush2_target
                    ]
                    if start_flush2_target is not None
                    else None
                )
                start_flush1_target = list(set_FR)
                flush_ramp_enabled_for_type = (
                    self.start_flush_ramp_enabled if flush_type_current == "start"
                    else (self.zero_flush_ramp_enabled if flush_type_current == "zero" else False)
                )
                is_start_flush_transition = (
                    flush_ramp_enabled_for_type
                    and is_flush_step
                    and flush_type_current in ("start", "zero")
                    and start_flush2_target is not None
                    and self.flush_time_s > 0
                )
                if flush_type_current in ("start", "zero"):
                    print(
                        "[MicrofluidicController] FlushRamp debug: "
                        f"transition_enabled={is_start_flush_transition}, "
                        f"flush_ramp_enabled={flush_ramp_enabled_for_type}, "
                        f"flush_time_s={self.flush_time_s}, "
                        f"flush_type={flush_type_current}, "
                        f"FR1={start_flush1_target}, FR2={start_flush2_target}"
                    )
                start_flush_transition_started_at = None
                start_flush_transition_completed = False
                start_flush_last_diag_t = 0.0
                start_flush_stable_reached_at = None
                start_flush_unstable_fallback_announced = False
                prime_to_start_ramp_active = (
                    just_transitioned_from_priming
                    and comp_idx == 0
                    and flush_type_current == "start"
                    and self._last_prime_set_fr_full is not None
                )
                prime_to_start_ramp_done = not prime_to_start_ramp_active
                prime_to_start_ramp_started_at = None
                prime_to_start_from = (
                    self._filter_flow_rate_for_active_channels(self._last_prime_set_fr_full, self.active_channels)
                    if prime_to_start_ramp_active else None
                )
                if prime_to_start_ramp_active and prime_to_start_from is not None:
                    # If an active lipid line was not part of the priming step, do not ramp it
                    # from zero/old value; start it directly at StartFlush1 for that channel.
                    primed_lines_set = set(self._last_primed_lines or set())
                    for i, ch in enumerate(self.active_channels):
                        if int(ch) == 1:
                            continue  # buffer channel is part of prime flush
                        line_num = int(ch) - 1
                        if line_num not in primed_lines_set and i < len(start_flush1_target):
                            prime_to_start_from[i] = float(start_flush1_target[i])
                if prime_to_start_ramp_active:
                    print(
                        "[MicrofluidicController] Prime->StartFlush1 ramp enabled: "
                        f"{self.prime_to_startflush_ramp_s:.1f}s from {prime_to_start_from} to {start_flush1_target}"
                    )
                kp_comp = np.array(self.K_p, dtype=float)
                if is_flush_step and len(kp_comp) > 0:
                    kp_comp[0] = kp_comp[0] / 50.0
                    print(
                        f"[MicrofluidicController] Flush composition {comp_idx}: "
                        f"buffer Kp reduced 50x ({self.K_p[0]:.6g} -> {kp_comp[0]:.6g})"
                    )
                seeded_zero_flush_with_start_pressures = False
                zero_flush_seed_pressures = None
                prev_had_zero_flow = False
                if comp_idx > 0 and (comp_idx - 1) < len(flow_rates):
                    prev_set_FR = flow_rates[comp_idx - 1]
                    prev_had_zero_flow = any(
                        (channel != 1 and i < len(prev_set_FR) and prev_set_FR[i] == 0)
                        for i, channel in enumerate(self.active_channels)
                    )

                # Seed zero flush with start-flush equilibrium pressures when available.
                if (
                    is_flush_step
                    and flush_type_current == "zero"
                    and self.zero_flow_blocking
                    and prev_had_zero_flow
                    and start_flush_equilibrium_pressures is not None
                    and len(start_flush_equilibrium_pressures) == len(self.active_channels)
                ):
                    try:
                        zero_flush_seed_pressures = np.array(start_flush_equilibrium_pressures, dtype=float)
                        seeded_zero_flush_with_start_pressures = True
                        print(
                            "[MicrofluidicController] Zero flush seeded from start-flush equilibrium pressures: "
                            + ", ".join(f"ch{ch}={zero_flush_seed_pressures[i]:.1f}" for i, ch in enumerate(self.active_channels))
                        )
                    except Exception:
                        pass
                
                # Set servo positions based on composition (close zero-composition lines to chip).
                # After priming, we can keep already-primed lines in waste for the first composition,
                # but if this experiment activates additional lines, update servos immediately.
                newly_active_after_priming = active_lines_set - set(self._last_primed_lines or set())
                should_open_servos = not (
                    just_transitioned_from_priming and comp_idx == 0 and not newly_active_after_priming
                )
                do_zero_flush_transition = (
                    seeded_zero_flush_with_start_pressures
                    and self.zero_flow_blocking
                    and self.zero_flush
                    and flush_type_current == "zero"
                )
                hold_zero_block_indices = set()
                zero_block_pressure_zero_after = {}
                if self.zero_flow_blocking and self.zero_flush and flush_type_current == "zero" and comp_idx > 0 and not do_zero_flush_transition:
                    prev_set_FR = flow_rates[comp_idx - 1] if (comp_idx - 1) < len(flow_rates) else []
                    hold_zero_block_indices = {
                        i for i, channel in enumerate(self.active_channels)
                        if channel != 1 and i < len(prev_set_FR) and prev_set_FR[i] == 0
                    }
                
                if should_open_servos:
                    # Debug: show servo decisions per line for this composition
                    try:
                        debug_lines = []
                        for i, channel in enumerate(self.active_channels):
                            if channel == 1:
                                continue
                            lipid_line = channel - 1
                            fr_val = set_FR[i] if i < len(set_FR) else None
                            if do_zero_flush_transition:
                                decision = "125(zero_flush_transition)"
                            elif i in hold_zero_block_indices:
                                decision = "40(hold_zero_flush)"
                            elif fr_val is None:
                                decision = "skip(no_fr)"
                            elif fr_val == 0:
                                decision = "40"
                            elif fr_val > 0:
                                decision = "125"
                            else:
                                decision = "neg"
                            debug_lines.append(f"L{lipid_line}: fr={fr_val} -> {decision}")
                        print(
                            f"[MicrofluidicController] Servo debug: active_channels={self.active_channels}, "
                            f"active_lines={active_lines}, set_FR={set_FR} | " + "; ".join(debug_lines)
                        )
                    except Exception:
                        pass
                    for i, channel in enumerate(self.active_channels):
                        if channel == 1:  # Buffer channel
                            continue
                        lipid_line = channel - 1  # map channels 2-4 -> lipid lines 1-3
                        if do_zero_flush_transition:
                            if self.ser:
                                try:
                                    expel.set_servo_angle(self.ser, lipid_line + 3, 125)
                                except Exception:
                                    pass
                        elif i in hold_zero_block_indices:
                            # Keep previously zero-blocked lines closed into the following zero flush.
                            if self.ser:
                                try:
                                    print(
                                        f"[Servo] Hold close to chip: servo {lipid_line + 3} "
                                        f"(line {lipid_line}) reason=zero_flush_hold_after_zero_block"
                                    )
                                    expel.set_servo_angle(self.ser, lipid_line + 3, 40)
                                    zero_block_pressure_zero_after[i] = time.time() + float(self.zero_block_hold_s)
                                except Exception:
                                    pass
                        elif i < len(set_FR) and set_FR[i] == 0:
                            # Zero flow: close to chip only if zero-flow blocking is enabled
                            if self.ser:
                                try:
                                    if self.zero_flow_blocking:
                                        print(
                                            f"[Servo] Close to chip: servo {lipid_line + 3} "
                                            f"(line {lipid_line}) reason=zero_flow_blocking"
                                        )
                                        expel.set_servo_angle(self.ser, lipid_line + 3, 40)
                                        zero_block_pressure_zero_after[i] = time.time() + float(self.zero_block_hold_s)
                                    else:
                                        expel.set_servo_angle(self.ser, lipid_line + 3, 125)
                                except Exception:
                                    pass
                        elif i < len(set_FR) and set_FR[i] > 0:
                            # Non-zero flow (active): close chip servo to waste (125 degrees) for flushing
                            if self.ser:
                                try:
                                    expel.set_servo_angle(self.ser, lipid_line + 3, 125)
                                except Exception:
                                    pass
                
                # Wait for servo valves to physically actuate before starting flow control
                print("[MicrofluidicController] Waiting 0.2s for servo valves to settle before flow control")
                time.sleep(0.2)

                if do_zero_flush_transition and zero_flush_seed_pressures is not None:
                    # Transition sequence for zero flush after a zero-flow composition:
                    # 1) Set all active pressures to 0
                    # 2) Open lipid valves to waste (125)
                    # 3) Restore to recorded start-flush equilibrium pressures
                    for i, channel in enumerate(self.active_channels):
                        for _ in range(3):
                            try:
                                if pump.set_pressure(channel, 0, self.calibarr) == 0:
                                    break
                            except Exception:
                                pass
                            time.sleep(0.05)
                    if self.ser:
                        for channel in self.active_channels:
                            if channel == 1:
                                continue
                            lipid_line = channel - 1
                            try:
                                expel.set_servo_angle(self.ser, lipid_line + 3, 125)
                            except Exception:
                                pass
                    time.sleep(0.2)
                    pr_control = np.array(zero_flush_seed_pressures, dtype=float)
                    I = np.array([0.0] * len(self.active_channels))
                    error_prev = np.array([0.0] * len(self.active_channels))
                    pr_history = [[float(pr_control[i])] for i in range(len(self.active_channels))]
                    for i, channel in enumerate(self.active_channels):
                        for _ in range(3):
                            try:
                                if pump.set_pressure(channel, float(pr_control[i]), self.calibarr) == 0:
                                    break
                            except Exception:
                                pass
                            time.sleep(0.05)
                    seeded_pressure_hold_until = time.time() + float(self.zero_block_hold_s)

                # Channels with zero lipid flow for this composition
                if self.zero_flow_blocking:
                    zero_lipid_indices = {
                        i for i, channel in enumerate(self.active_channels)
                        if channel != 1 and i < len(set_FR) and set_FR[i] == 0
                    }
                else:
                    zero_lipid_indices = set()
                
                # Track total volume flow for ALL channels during entire experiment
                lipid_total_flow = [0.0, 0.0, 0.0]  # Lipid lines 1-3 (channels 2-4)
                
                lipid_fr_err_clip = [[] for _ in range(len(self.active_channels) - 1)]
                buffer_fr_err_clip = []
                
                # Initialize flow data storage for this composition
                comp_time_data = []
                comp_flows = [[], [], [], []]  # 4 channels
                comp_pressures_set = [[], [], [], []]
                comp_pressures_act = [[], [], [], []]
                comp_extra_flows = []
                comp_extra_pressures_set = []
                comp_extra_pressures_act = []
                flowstart_idx = 0  # Index where equilibration completes and collection starts
                
                eq_start = time.time()
                eq_timeout = params.get("eq_max_t", 180)
                maxfrerror = params.get("maxfrerror", [100, 0.2])
                
                start_t = time.time()
                last_t = start_t
                # For zero flush seeded with start-flush pressures:
                # keep seeded pressures for configured hold time before PI resumes.
                seeded_pressure_hold_until = (
                    start_t + float(self.zero_block_hold_s)
                ) if seeded_zero_flush_with_start_pressures else 0.0
                
                # Allow expul_t to be overridden (for prime_flush), otherwise calculate
                if "expul_t" in params:
                    expul_t = float(params["expul_t"])
                    print(f"Expulsion time overridden: {expul_t}s")
                else:
                    expul_t = self._calculate_expulsion_time(params.get("tubingdim", [0.51, 750]), set_FR)
                    print("Expulsion time calculated:", expul_t)
                
                if orig_idx == 0 and self.first_comp_delay_s > 0:
                    expul_t += self.first_comp_delay_s
                    print(f"[MicrofluidicController] First composition delay added: +{self.first_comp_delay_s:.1f}s (expul_t={expul_t:.1f}s)")
                _flush_expul_added = False
                start_flush_ramp_earliest_t = start_t + float(expul_t)
                
                while True:
                    # Handle stop
                    if self._stop_requested:
                        self._end_collection_to_waste()
                        self._stop_flows()
                        self.status_broker.set_microfluidic_state("Stopped")
                        self.status_broker.set_current_experiment(None, 0)
                        return
                    
                    # Handle pause
                    while self.is_paused:
                        self._stop_flows()
                        self._end_collection_to_waste()
                        time.sleep(0.1)
                        if not self.is_running:
                            return

                    # On resume, require re-equilibration
                    if self._re_eq_required:
                        self._re_eq_required = False
                        collection = False
                        start_t = time.time()
                        last_t = start_t
                        lipid_fr_err_clip = [[] for _ in range(len(self.active_channels) - 1)]
                        buffer_fr_err_clip = []

                    # Handle skip
                    if self.should_skip:
                        self.should_skip = False
                        self._end_collection_to_waste()
                        break

                    # Read flows with retry logic
                    # Calculate time interval using fixed period (not actual elapsed, which includes processing overhead)
                    interval = self.period  # Use fixed 0.5s period for all volume calculations
                    now_t = time.time()
                    seeded_hold_active = seeded_zero_flush_with_start_pressures and (now_t < seeded_pressure_hold_until)
                    flows = []
                    for i, channel in enumerate(self.active_channels):
                        if i in zero_lipid_indices:
                            # Zero lipid flow: close to chip already, force pressure/flow to zero
                            flows.append(0.0)
                            lipid_fr_err_clip[i-1].append(0.0)
                            # Ensure valve closure is settled before forcing pressure to zero.
                            zero_after_t = float(zero_block_pressure_zero_after.get(i, 0.0))
                            allow_zero_pressure = (now_t >= zero_after_t)
                            if allow_zero_pressure:
                                pr_control[i] = 0.0
                                I[i] = 0.0
                                error_prev[i] = 0.0
                                pr_history[i].append(0.0)
                            # Set pressure (0 only after closure delay; otherwise hold current)
                            target_p = 0.0 if allow_zero_pressure else float(pr_control[i])
                            pressure_set = False
                            for retry in range(3):
                                try:
                                    err = pump.set_pressure(channel, target_p, self.calibarr)
                                    if err == 0:
                                        pressure_set = True
                                        break
                                    time.sleep(0.05)
                                except Exception:
                                    time.sleep(0.05)
                            if not pressure_set:
                                print(f"[Warning] Channel {channel} pressure set failed - continuing")
                            continue

                        # Retry sensor reading up to 3 times
                        fr = None
                        sensor_error = None
                        for retry in range(3):
                            try:
                                fr_raw, err = pump.get_sensor_data(channel)
                                if err == 0:
                                    fr = fr_raw
                                    break
                                else:
                                    sensor_error = err
                                    time.sleep(0.05)  # Brief delay before retry
                            except Exception as e:
                                sensor_error = str(e)
                                time.sleep(0.05)
                        
                        if fr is None:
                            # Sensor read failed after retries - use last known value or 0
                            print(f"[Warning] Channel {channel} sensor error {sensor_error} - using last flow")
                            fr = flows[i] if i < len(flows) and flows else 0.0
                        
                        # Apply correction polynomial
                        # Buffer (channel 1) always uses normal correction
                        # Lipids use absolute value for negative readings (backflow)
                        if channel == 1:
                            fr = self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                 self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                 self.sensorcorr[channel-1][4]
                        elif fr > 0:
                            fr = self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                 self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                 self.sensorcorr[channel-1][4]
                        else:
                            # Lipid channel with negative flow - apply absolute value
                            fr = -abs(self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                  self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                  self.sensorcorr[channel-1][4])
                        flows.append(fr)
                        
                        # Calculate error
                        fr_error = fr - set_FR[i]
                        if channel == 1:
                            buffer_fr_err_clip.append(abs(fr_error))
                        else:
                            lipid_fr_err_clip[i-1].append(abs(fr_error))
                        
                        # PI control with linear interpolation (matching working code)
                        if seeded_hold_active:
                            # Hold seeded start-flush pressures for zero flush startup window.
                            pr_control[i] = float(np.clip(pr_control[i], self.p_range[0], self.p_range[1]))
                        else:
                            if error_prev[i] * fr_error < 0 and len(pr_history[i]) > 2:
                                # Error crossed zero - use linear interpolation to find optimal pressure
                                pr_control[i] = pr_history[i][-2] + \
                                    (((pr_history[i][-1] - pr_history[i][-2]) / (fr_error - error_prev[i])) * (0 - error_prev[i]))
                                pr_control[i] = np.clip(pr_control[i], self.p_range[0], self.p_range[1])
                            else:
                                # Standard PI control
                                I[i] = I[i] + self.K_i * fr_error * self.period
                                adjustment = (fr_error * abs(fr_error)) * kp_comp[i] + I[i]
                                adjustment = np.clip(adjustment, self.p_incr[0], self.p_incr[1])
                                pr_control[i] = pr_control[i] - adjustment
                                pr_control[i] = np.clip(pr_control[i], self.p_range[0], self.p_range[1])
                        
                        # Store pressure history for next iteration's interpolation
                        pr_history[i].append(pr_control[i])
                        
                        # Set pressure with retry logic
                        pressure_set = False
                        for retry in range(3):
                            try:
                                err = pump.set_pressure(channel, pr_control[i], self.calibarr)
                                if err == 0:
                                    pressure_set = True
                                    break
                                else:
                                    time.sleep(0.05)
                            except Exception as e:
                                time.sleep(0.05)
                        
                        if not pressure_set:
                            print(f"[Warning] Channel {channel} pressure set failed - continuing")
                        
                        error_prev[i] = fr_error

                    if rna_on_extra_controller:
                        extra_flow_val = self.extra_flow_last if self.extra_flow_last is not None else 0.0
                        sensor_error = None
                        for retry in range(3):
                            try:
                                fr_raw, err = self.extra_pump.get_flow()
                                if fr_raw is not None and not err:
                                    extra_flow_val = self._correct_extra_rna_flow(float(fr_raw))
                                    self.extra_flow_last = float(extra_flow_val)
                                    break
                                sensor_error = err
                                time.sleep(0.05)
                            except Exception as e:
                                sensor_error = str(e)
                                time.sleep(0.05)
                        else:
                            print(f"[Warning] Extra RNA sensor error {sensor_error} - using last flow")

                        extra_fr_error = float(extra_flow_val) - float(extra_rna_set_fr)
                        buffer_fr_err_clip.append(abs(extra_fr_error))
                        extra_kp = float(kp_comp[3] if len(kp_comp) > 3 else kp_comp[-1])
                        if seeded_hold_active:
                            extra_pressure = float(np.clip(extra_pressure, extra_pmin, extra_pmax_safe))
                        elif extra_error_prev * extra_fr_error < 0 and len(extra_pr_history) > 1:
                            extra_pressure = extra_pr_history[-2] + (
                                ((extra_pr_history[-1] - extra_pr_history[-2]) / (extra_fr_error - extra_error_prev))
                                * (0 - extra_error_prev)
                            )
                            extra_pressure = float(np.clip(extra_pressure, extra_pmin, extra_pmax_safe))
                        else:
                            extra_i_term = extra_i_term + self.K_i * extra_fr_error * self.period
                            extra_adjustment = (extra_fr_error * abs(extra_fr_error)) * extra_kp + extra_i_term
                            extra_adjustment = float(np.clip(extra_adjustment, self.p_incr[0], self.p_incr[1]))
                            extra_pressure = float(np.clip(extra_pressure - extra_adjustment, extra_pmin, extra_pmax_safe))
                        ok, err = self.extra_pump.set_pressure(extra_pressure)
                        if not ok:
                            print(f"[Warning] Extra RNA pressure set failed: {err}")
                        self.extra_pressure_last = float(extra_pressure)
                        extra_pr_history.append(float(extra_pressure))
                        extra_error_prev = float(extra_fr_error)
                    
                    # Read pressures with retry logic
                    pressures_act = []
                    for i, channel in enumerate(self.active_channels):
                        if i in zero_lipid_indices:
                            pressures_act.append(0.0)
                            continue
                        p = None
                        for retry in range(3):
                            try:
                                p_val, err = pump.get_pressure_data(channel, self.calibarr)
                                if err == 0:
                                    p = p_val
                                    break
                                else:
                                    time.sleep(0.05)
                            except Exception as e:
                                time.sleep(0.05)
                        
                        if p is None:
                            # Use set pressure as fallback
                            p = pr_control[channel_to_index[channel]]
                            print(f"[Warning] Channel {channel} pressure read failed - using setpoint")

                        pressures_act.append(p)

                    extra_flow_val = self.extra_flow_last if rna_on_extra_controller else None
                    extra_pressure_set_val = float(extra_pressure) if rna_on_extra_controller else None
                    extra_pressure_act_val = extra_pressure_set_val
                    
                    # Log data (non-blocking: append_flow_reading is now a no-op)
                    full_flows = self._map_active_values_to_full(self.active_channels, flows)
                    full_p_set = self._map_active_values_to_full(self.active_channels, list(pr_control))
                    full_p_act = self._map_active_values_to_full(self.active_channels, pressures_act)
                    self.data_logger.append_flow_reading(
                        exp_id,
                        time.time(),
                        full_flows,
                        full_p_act,
                        pressures_set=full_p_set,
                        extra_flow=extra_flow_val,
                        extra_pressure_act=extra_pressure_act_val,
                        extra_pressure_set=extra_pressure_set_val,
                    )
                    self.status_broker.update_flow_data(
                        time.time() - init_start_t,
                        full_flows,
                        full_p_set,
                        full_p_act,
                        extra_flow=extra_flow_val,
                        extra_p_set=extra_pressure_set_val,
                        extra_p_act=extra_pressure_act_val,
                    )
                    self.status_broker.set_live_flows(full_flows, extra_flow_val, extra_flow_enabled)
                    
                    # Store flow data locally for this composition (in-memory, non-blocking)
                    comp_time_data.append(time.time() - init_start_t)
                    for ch_idx in range(4):
                        comp_flows[ch_idx].append(full_flows[ch_idx])
                        comp_pressures_set[ch_idx].append(full_p_set[ch_idx])
                        comp_pressures_act[ch_idx].append(full_p_act[ch_idx])
                    comp_extra_flows.append(extra_flow_val)
                    comp_extra_pressures_set.append(extra_pressure_set_val)
                    comp_extra_pressures_act.append(extra_pressure_act_val)
                    
                    # Check equilibration using a recent rolling window so startup spikes
                    # do not keep stability false indefinitely.
                    stable_window_n = max(1, int(round(5.0 / max(float(self.period), 0.001))))
                    max_lipid_fr_err = np.max([
                        np.max(errors[-stable_window_n:]) if errors else 0
                        for errors in lipid_fr_err_clip
                    ])
                    max_buffer_fr_err = (
                        np.max(buffer_fr_err_clip[-stable_window_n:])
                        if buffer_fr_err_clip
                        else 0
                    )
                    
                    # Check if this is a flush composition (skip collection)
                    is_flush = is_flush_step
                    flush_type = flush_type_current

                    if is_flush and self.flush_time_s > 0 and not _flush_expul_added and not is_start_flush_transition:
                        expul_t += self.flush_time_s
                        _flush_expul_added = True
                        print(
                            f"[MicrofluidicController] Flush equilibration time added: +{self.flush_time_s:.1f}s "
                            f"(type={flush_type or 'volume0'} expul_t={expul_t:.1f}s)"
                        )
                    
                    # Also treat as flush if volume is 0 (priming/flushing with no collection)
                    if self.target_volume == 0 and not force_collect:
                        is_flush = True
                    
                    # Check if flow rates are stable and show countdown if waiting for expul_t
                    if prime_to_start_ramp_active and not prime_to_start_ramp_done:
                        if prime_to_start_ramp_started_at is None:
                            prime_to_start_ramp_started_at = time.time()
                        r_elapsed = time.time() - prime_to_start_ramp_started_at
                        alpha_r = float(np.clip(r_elapsed / max(float(self.prime_to_startflush_ramp_s), 0.001), 0.0, 1.0))
                        set_FR = [
                            float((1.0 - alpha_r) * fr0 + alpha_r * fr1)
                            for fr0, fr1 in zip(prime_to_start_from, start_flush1_target)
                        ]
                        self.status_broker.set_microfluidic_state(
                            f"Prime->StartFlush ramping ({max(0.0, self.prime_to_startflush_ramp_s - r_elapsed):.0f}s)"
                        )
                        if alpha_r >= 1.0:
                            prime_to_start_ramp_done = True
                            # Start expulsion timer after the prime->flush ramp has completed.
                            start_t = time.time()
                            last_t = start_t
                            # Earliest possible ramp gate; final gate is based on
                            # expulsion time counted from first stable detection.
                            start_flush_ramp_earliest_t = start_t
                            print("[MicrofluidicController] Prime->StartFlush1 ramp complete; starting expulsion hold timer")

                    elapsed_t = time.time() - start_t
                    flow_stable = max_lipid_fr_err < maxfrerror[1] and max_buffer_fr_err < maxfrerror[0]
                    if (
                        is_start_flush_transition
                        and prime_to_start_ramp_done
                        and flow_stable
                        and start_flush_stable_reached_at is None
                    ):
                        start_flush_stable_reached_at = time.time()
                        print(
                            "[MicrofluidicController] FlushRamp stable reached; "
                            f"starting expulsion hold ({expul_t:.1f}s) before ramp"
                        )
                    if is_start_flush_transition and (time.time() - start_flush_last_diag_t) >= 10.0:
                        start_flush_last_diag_t = time.time()
                        print(
                            "[MicrofluidicController] FlushRamp state: "
                            f"elapsed={elapsed_t:.1f}s, expul_t={expul_t:.1f}s, "
                            f"stable={flow_stable}, ramp_started={start_flush_transition_started_at is not None}, "
                            f"ramp_done={start_flush_transition_completed}, "
                            f"stable_latched={start_flush_stable_reached_at is not None}"
                        )
                    # StartFlush1->StartFlush2 ramp starts after the standard
                    # expulsion/equilibration window has elapsed.
                    # Prefer stable-latched timing, but do not deadlock if
                    # stability is never reached in noisy/high-error runs.
                    fallback_expul_gate_ok = (
                        start_flush_stable_reached_at is None
                        and elapsed_t >= float(expul_t)
                    )
                    if (
                        is_start_flush_transition
                        and prime_to_start_ramp_done
                        and fallback_expul_gate_ok
                        and not start_flush_unstable_fallback_announced
                    ):
                        start_flush_unstable_fallback_announced = True
                        print(
                            "[MicrofluidicController] FlushRamp fallback: "
                            f"stability not reached by expul_t={expul_t:.1f}s; "
                            "starting timed ramp to FR2."
                        )
                    stable_expul_gate_ok = (
                        start_flush_stable_reached_at is not None
                        and (now_t - start_flush_stable_reached_at) >= float(expul_t)
                    ) or fallback_expul_gate_ok
                    if (
                        is_start_flush_transition
                        and prime_to_start_ramp_done
                        and now_t >= start_flush_ramp_earliest_t
                        and stable_expul_gate_ok
                    ):
                        if start_flush_transition_started_at is None:
                            start_flush_transition_started_at = time.time()
                            print(
                                f"[MicrofluidicController] Flush transition ({flush_type_current}): "
                                f"ramping to FR2 over {self.flush_time_s:.1f}s"
                            )
                        t_ramp = (time.time() - start_flush_transition_started_at) / max(float(self.flush_time_s), 0.001)
                        alpha = float(np.clip(t_ramp, 0.0, 1.0))
                        set_FR = [
                            float((1.0 - alpha) * fr1 + alpha * fr2)
                            for fr1, fr2 in zip(start_flush1_target, start_flush2_target)
                        ]
                        if alpha >= 1.0 and not start_flush_transition_completed:
                            start_flush_transition_completed = True
                            print(
                                f"[MicrofluidicController] Flush transition complete ({flush_type_current}) "
                                "(at FR2 setpoints)"
                            )
                    
                    if not collection:
                        if prime_to_start_ramp_active and not prime_to_start_ramp_done:
                            pass
                        elif is_start_flush_transition and start_flush_transition_started_at is not None and not start_flush_transition_completed:
                            ramp_left = max(0.0, float(self.flush_time_s) - (time.time() - start_flush_transition_started_at))
                            self.status_broker.set_microfluidic_state(f"Flush ramping ({ramp_left:.0f}s)")
                        elif flow_stable and elapsed_t <= expul_t:
                            # Flow is stable but waiting for expulsion time
                            countdown = expul_t - elapsed_t
                            self.status_broker.set_microfluidic_state(f"Stable - Expelling ({countdown:.0f}s)")
                        elif not flow_stable:
                            # Still equilibrating
                            self.status_broker.set_microfluidic_state(f"Equilibrating (L:{max_lipid_fr_err:.2f} B:{max_buffer_fr_err:.1f})")
                    
                    if not prime_to_start_ramp_done:
                        ready_for_completion = False
                    elif is_start_flush_transition:
                        # For ramped flushes, completion is driven by ramp finish.
                        ready_for_completion = start_flush_transition_completed
                    else:
                        ready_for_completion = (elapsed_t > expul_t)
                    if ready_for_completion and not collection:
                        # Ramped flushes should end immediately at ramp completion.
                        if is_flush and is_start_flush_transition:
                            if (
                                flush_type_current == "start"
                                and start_flush_equilibrium_pressures is None
                            ):
                                start_flush_equilibrium_pressures = list(pr_control)
                                print(
                                    "[MicrofluidicController] Recorded start-flush end pressures: "
                                    + ", ".join(
                                        f"ch{ch}={start_flush_equilibrium_pressures[i]:.1f}"
                                        for i, ch in enumerate(self.active_channels)
                                    )
                                )
                            print(
                                f"[MicrofluidicController] Ramped flush {comp_idx} complete - "
                                "skipping collection immediately"
                            )
                            break
                        if max_lipid_fr_err < maxfrerror[1] and max_buffer_fr_err < maxfrerror[0]:
                            self.status_broker.set_microfluidic_state("Equilibrated")

                            if (
                                is_flush_step
                                and flush_type_current == "start"
                                and start_flush_equilibrium_pressures is None
                            ):
                                start_flush_equilibrium_pressures = list(pr_control)
                                print(
                                    "[MicrofluidicController] Recorded start-flush equilibrium pressures: "
                                    + ", ".join(
                                        f"ch{ch}={start_flush_equilibrium_pressures[i]:.1f}"
                                        for i, ch in enumerate(self.active_channels)
                                    )
                                )

                            extra_hold = 0.0
                            if is_flush and hold_after_eq > 0:
                                extra_hold = hold_after_eq

                            if extra_hold > 0:
                                hold_start = time.time()
                                print(f"[MicrofluidicController] Equilibrated - holding for {extra_hold}s")
                                # Continue PI control loop during hold
                                while time.time() - hold_start < extra_hold:
                                    if self._stop_requested:
                                        break
                                    
                                    hold_elapsed = time.time() - hold_start
                                    self.status_broker.set_microfluidic_state(f"Holding: {hold_elapsed:.1f}/{extra_hold:.0f}s")
                                    
                                    # Read flows and continue PI control
                                    flows = []
                                    for i, channel in enumerate(self.active_channels):
                                        if i in zero_lipid_indices:
                                            flows.append(0.0)
                                            continue
                                        fr = None
                                        for retry in range(3):
                                            try:
                                                fr_raw, err = pump.get_sensor_data(channel)
                                                if err == 0:
                                                    fr = fr_raw
                                                    break
                                                time.sleep(0.05)
                                            except Exception:
                                                time.sleep(0.05)
                                        
                                        if fr is None:
                                            fr = flows[i] if i < len(flows) else 0.0
                                        
                                        # Apply correction
                                        if channel == 1:
                                            fr = self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                                 self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                                 self.sensorcorr[channel-1][4]
                                        elif fr > 0:
                                            fr = self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                                 self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                                 self.sensorcorr[channel-1][4]
                                        else:
                                            fr = -abs(self.sensorcorr[channel-1][0]*(fr**4) + self.sensorcorr[channel-1][1]*(fr**3) + \
                                                  self.sensorcorr[channel-1][2]*(fr**2) + self.sensorcorr[channel-1][3]*fr + \
                                                  self.sensorcorr[channel-1][4])
                                        flows.append(fr)
                                    
                                    # Apply PI control to maintain flows
                                    interval = self.period
                                    errors = np.array([set_FR[i] - flows[i] for i in range(len(flows))])
                                    P = kp_comp[:len(errors)] * errors
                                    I += self.K_i * errors * interval
                                    pr_control += (P + I)
                                    pr_control = np.clip(pr_control, self.p_range[0], self.p_range[1])

                                    # Force zero pressure for zero-flow lipid channels
                                    for i in zero_lipid_indices:
                                        pr_control[i] = 0.0
                                        I[i] = 0.0
                                    
                                    for ch in self.active_channels:
                                        ch_idx = channel_to_index[ch]
                                        if ch_idx in zero_lipid_indices:
                                            pump.set_pressure(ch, 0, self.calibarr)
                                        else:
                                            pump.set_pressure(ch, int(pr_control[ch_idx]), self.calibarr)
                                    
                                    # Update plots
                                    pressures_act = []
                                    for i, channel in enumerate(self.active_channels):
                                        # Use pump.get_pressure_data; fall back to set pressure on error
                                        if i in zero_lipid_indices:
                                            pressures_act.append(0.0)
                                            continue
                                        p_val, p_err = pump.get_pressure_data(channel, self.calibarr)
                                        if p_err != 0 or p_val is None:
                                            p_val = pr_control[channel_to_index[channel]]
                                        pressures_act.append(p_val)
                                    full_flows = self._map_active_values_to_full(self.active_channels, flows)
                                    full_p_set = self._map_active_values_to_full(self.active_channels, list(pr_control))
                                    full_p_act = self._map_active_values_to_full(self.active_channels, pressures_act)
                                    extra_flow_val = self.extra_flow_last if rna_on_extra_controller else None
                                    extra_pressure_set_val = float(extra_pressure) if rna_on_extra_controller else None
                                    extra_pressure_act_val = extra_pressure_set_val
                                    self.status_broker.update_flow_data(
                                        time.time() - init_start_t,
                                        full_flows,
                                        full_p_set,
                                        full_p_act,
                                        extra_flow=extra_flow_val,
                                        extra_p_set=extra_pressure_set_val,
                                        extra_p_act=extra_pressure_act_val,
                                    )
                                    self.status_broker.set_live_flows(full_flows, extra_flow_val, extra_flow_enabled)
                                    
                                    time.sleep(interval)
                                
                                print(f"[MicrofluidicController] Hold complete after {extra_hold}s")
                            
                            if is_flush:
                                print(
                                    f"[MicrofluidicController] Flush composition {comp_idx} equilibrated - skipping collection "
                                    f"(start_flush_ramp_done={start_flush_transition_completed}, elapsed={elapsed_t:.1f}s)"
                                )
                                break  # Exit main while loop - flush composition done

                            # Non-flush composition: proceed to collection mode
                            collection = True
                            collection_start_time = time.time()  # Record when collection actually starts
                            self.last_stable_pressures = list(pr_control)
                            
                            # Mark the index where equilibration ends (for steady-state flow analysis)
                            flowstart_idx = len(comp_time_data)

                            # Mark collection start for plot (dotted line)
                            self.status_broker.add_collection_marker(time.time() - init_start_t)
                            
                            # Reset integral term to prevent windup from equilibration phase
                            I = np.array([0.0, 0.0, 0.0, 0.0])
                            
                            # Move to collection mode (background thread)
                            if self.ser and not skip_collect_move:
                                def _move_to_collect():
                                    expel.servoswitch(self.ser, 1)
                                    expel.movez(self.ser, "Down", 1300, 400)
                                self.hw_executor.submit(_move_to_collect)
                            
                            # Reduce gains for collection
                            self.K_p = self.K_p / 5
                            self.K_i = self.K_i / 10
                            
                            lipid_fr_err_clip = [[] for _ in range(len(self.active_channels) - 1)]
                            buffer_fr_err_clip = []
                        else:
                            # Trim old errors
                            for i in range(len(lipid_fr_err_clip)):
                                if lipid_fr_err_clip[i]:
                                    lipid_fr_err_clip[i] = lipid_fr_err_clip[i][1:]
                            if buffer_fr_err_clip:
                                buffer_fr_err_clip = buffer_fr_err_clip[1:]
                    
                    # Update collection volume
                    if collection:
                        self.collected_volume += (np.sum(flows) / 60) * interval
                        self.status_broker.set_collection_progress(self.collected_volume, self.target_volume)
                        self.status_broker.set_microfluidic_state(
                            f"Collecting ({self.collected_volume:.1f}/{self.target_volume}µL)"
                        )
                    
                    # Track ALL lipid flow (equilibration + collection) for volume depletion
                    # Only deplete if line is actually loaded (skip admin mode priming)
                    for i, channel in enumerate(self.active_channels[1:], start=1):
                        vol_flow = (flows[i] / 60) * interval
                        lipid_line = channel - 1  # map channels 2-4 -> lipid lines 1-3
                        lipid_total_flow[lipid_line - 1] += vol_flow
                        
                        # Check if line is loaded before depleting
                        line_state = self.lipid_tracker.get_line_state(lipid_line)
                        if line_state.get("lipid_name") is not None and line_state.get("remaining_volume", 0) > 0:
                            # Line is loaded - deplete volume
                            self.lipid_tracker.deplete_line(lipid_line, vol_flow)
                            line_state = self.lipid_tracker.get_line_state(lipid_line)
                            if line_state["remaining_volume"] <= 0:
                                lipid_name = line_state.get("lipid_name") or f"line {lipid_line}"
                                self.status_broker.set_error(f"Lipid depleted: {lipid_name}")
                                self.pause()

                    # Check completion - use time-based calculation with set flow rates
                    collection_elapsed = (time.time() - collection_start_time) if collection_start_time else 0
                    time_based_complete = collection and (collection_elapsed >= expected_collection_time)
                    # Skip volume check for flush compositions (volume=0)
                    volume_based_complete = collection and self.target_volume > 0 and self.collected_volume >= self.target_volume
                    eq_timeout_hit = (not collection) and ((time.time() - start_t) > eq_timeout)

                    if eq_timeout_hit and not is_flush:
                        retry_key = orig_idx if orig_idx is not None else comp_idx
                        if self.equilibration_retry and retry_key not in eq_retry_used:
                            eq_retry_used.add(retry_key)
                            print(
                                f"[MicrofluidicController] Equilibration timeout on composition {retry_key}. "
                                "Scheduling retry flush then retrying same well."
                            )
                            # Replace current composition with a retry flush, then insert
                            # the original composition immediately after it.
                            original_fr = list(flow_rates[comp_idx])
                            flow_rates[comp_idx] = list(active_start_flush1_frs)
                            comp_idx_map[comp_idx] = None
                            flush_kind[comp_idx] = "retry"
                            if comp_idx < len(start_flush2_targets):
                                start_flush2_targets[comp_idx] = list(active_start_flush2_frs)
                            flow_rates.insert(comp_idx + 1, original_fr)
                            comp_idx_map.insert(comp_idx + 1, orig_idx)
                            flush_kind.insert(comp_idx + 1, None)
                            start_flush2_targets.insert(comp_idx + 1, None)
                            self._end_collection_to_waste()
                            self.status_broker.set_microfluidic_state("Retrying composition after flush")
                            break
                        elif self.equilibration_retry:
                            msg = (
                                f"Equilibration retry failed for composition {retry_key}. "
                                "Stopping experiment."
                            )
                            print(f"[MicrofluidicController] {msg}")
                            self._enter_fail_safe(msg)
                            return
                    
                    if time_based_complete or volume_based_complete or eq_timeout_hit:
                        # Start end-of-collection hardware movements in background
                        # (Z up and servo to waste mode take ~1.75s, let them run in parallel)
                        self._end_collection_to_waste()

                        # Restore gains immediately (hardware moves in background)
                        self.K_p = self.K_p * 5
                        self.K_i = self.K_i * 10

                        # Only log and mark complete if not a flush composition
                        if not is_flush:
                            comp_log_idx = orig_idx if orig_idx is not None else comp_idx
                            
                            # Store flow data for record keeping (in-memory copy, non-blocking)
                            self.data_logger.store_composition_flow_data(
                                exp_id=exp_id,
                                comp_idx=comp_log_idx,
                                time_data=comp_time_data,
                                flows=comp_flows,
                                pressures_set=comp_pressures_set,
                                pressures_act=comp_pressures_act,
                                extra_flow=comp_extra_flows,
                                extra_pressure_set=comp_extra_pressures_set,
                                extra_pressure_act=comp_extra_pressures_act,
                                set_FR=set_FR,
                                flowstart=flowstart_idx
                            )
                            
                            # Save to disk (runs in background thread, non-blocking)
                            self.data_logger.append_collection_event(exp_id, comp_log_idx, self.collected_volume)

                            # Mark composition as completed
                            if self.on_composition_complete:
                                try:
                                    self.on_composition_complete(exp_id, comp_log_idx)
                                except Exception as e:
                                    print(f"[MicrofluidicController] Error updating composition status: {e}")

                            if autocollect and self.ser and comp_idx < len(flow_rates) - 1:
                                # Do not advance wells for flush-only compositions
                                if orig_idx is None:
                                    break
                                if output_wells and orig_idx is not None and orig_idx + 1 < len(output_wells):
                                    next_target = output_wells[orig_idx + 1]
                                    if self.zero_flush:
                                        current_well = self._move_to_well_sync(current_well, next_target)
                                    else:
                                        current_well = self._queue_move_to_well(current_well, next_target)
                                    wpcurrent = [current_well[1], current_well[2]]
                                    # Update status broker with new well position
                                    self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
                                    self._current_well = current_well
                                else:
                                    wpcurrent = self._advance_well(wpcurrent, wpdim)
                                    target_well = (current_well[0], wpcurrent[0], wpcurrent[1])
                                    if self.zero_flush:
                                        current_well = self._move_to_well_sync(current_well, target_well)
                                    else:
                                        current_well = self._queue_move_to_well(current_well, target_well)
                                    # Update status broker with new well position
                                    self.status_broker.set_current_well(wpcurrent[0], wpcurrent[1], current_well[0])
                                    self._current_well = current_well
                        break
                    
                    # Sleep to maintain loop period
                    sleep_t = self.period - (time.time() - last_t)
                    if sleep_t > 0:
                        time.sleep(sleep_t)
                    last_t = time.time()  # Update for next iteration's sleep calculation
                
                # Reset plot data after each composition (including flush)
                self.status_broker.clear_flow_data()

                # After each composition, transition to next without cutting pressure
                # The PI controller will naturally adjust pressures for the new flow rates
                if comp_idx < len(flow_rates) - 1:
                    # More compositions to go
                    self.status_broker.set_microfluidic_state(f"Next composition ({comp_idx + 2}/{len(flow_rates)})")
                    time.sleep(1)  # Brief pause between compositions
            
            # All compositions complete - clear experiment and set idle/ready
            if exp_id == "prime_flush":
                # Transition to the next experiment without cutting pressure
                self.status_broker.set_current_experiment(None, 0)
                self.status_broker.set_microfluidic_state("Ready")
                print(f"[MicrofluidicController] Prime flush completed - keeping pressures for next experiment")
            else:
                # Stop all pressures at end of experiment
                self._stop_flows()
                self.status_broker.set_current_experiment(None, 0)
                self.status_broker.set_microfluidic_state("Idle")
                print(f"[MicrofluidicController] Experiment {exp_id} completed all {len(flow_rates)} compositions")
            
            # Notify controller that experiment is complete (skip for prime_flush)
            if self.on_experiment_complete and exp_id != "prime_flush":
                try:
                    self.on_experiment_complete(exp_id)
                except Exception as e:
                    print(f"[MicrofluidicController] Error in on_experiment_complete callback: {e}")
            
        except Exception as e:
            self._enter_fail_safe(f"Experiment failed: {e}")
            raise

    def _stop_flows(self):
        """Stop all pressures."""
        for ch in (1, 2, 3, 4):
            pump.set_pressure(ch, 0, self.calibarr)
        self._set_extra_pressure_zero()

    def stop_all_pressures(self):
        """Public helper to stop all pressures safely."""
        try:
            if self.calibarr is None:
                return
            self._stop_flows()
        except Exception:
            pass

    def shutdown(self):
        """Best-effort controller shutdown: stop loop/flows and close extra SDK session."""
        try:
            self._stop_requested = True
            self.stop_experiment()
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.stop_all_pressures()
        except Exception:
            pass
        try:
            if self.extra_pump_connected:
                self.extra_pump.disconnect()
            self.extra_pump_connected = False
            self.status_broker.set_connection_state("extra_pressure", False)
        except Exception:
            pass

    def _calculate_expulsion_time(self, tubingdim: List[float], set_FR: List[float]) -> float:
        """Calculate expulsion time based on tubing dimensions."""
        import math
        area = math.pi * (tubingdim[0] / 2) ** 2
        tube_vol = area * (tubingdim[1] * 1.2)
        return tube_vol / (np.sum(set_FR) / 60)

    def _end_collection_to_waste(self):
        """End collection: move Z up and switch to waste mode (blocking)."""
        if self.ser:
            expel.movez(self.ser, "Up", 1300, 400)
            expel.servoswitch(self.ser, 0)

    def _advance_well(self, wpcurrent, wpdim):
        """Advance to next well (row-wise)."""
        r, c = wpcurrent
        if c >= wpdim[1]:
            return [r + 1, 1]
        return [r, c + 1]

    def _enter_fail_safe(self, reason: str):
        """Fail-safe: stop pressures and move to waste mode."""
        self._stop_flows()
        self._end_collection_to_waste()
        self.status_broker.set_live_flows([None, None, None, None], self.extra_flow_last, False)
        self.status_broker.set_error(reason)
        self.status_broker.set_microfluidic_state("FailSafe")

    def apply_config(self, cfg: dict):
        import ast
        self._config = cfg  # Store full config for later access
        self.period = float(cfg.get("period", self.period))
        
        # Parse K_p - handle both list and string representations
        kp_val = cfg.get("K_p", self.K_p)
        if isinstance(kp_val, str):
            try:
                kp_val = ast.literal_eval(kp_val)
            except:
                pass
        self.K_p = np.array(kp_val)
        
        self.K_i = float(cfg.get("K_i", self.K_i))
        
        # Parse p_incr
        p_incr_val = cfg.get("p_incr", self.p_incr)
        if isinstance(p_incr_val, str):
            try:
                p_incr_val = ast.literal_eval(p_incr_val)
            except:
                pass
        self.p_incr = p_incr_val
        
        # Parse p_range
        p_range_val = cfg.get("p_range", self.p_range)
        if isinstance(p_range_val, str):
            try:
                p_range_val = ast.literal_eval(p_range_val)
            except:
                pass
        self.p_range = p_range_val
        
        # Parse sensorcorr
        sensorcorr_val = cfg.get("sensorcorr", self.sensorcorr)
        if isinstance(sensorcorr_val, str):
            try:
                sensorcorr_val = ast.literal_eval(sensorcorr_val)
            except:
                pass
        self.sensorcorr = sensorcorr_val
        try:
            line3_rna_mode = bool(cfg.get("line3_RNA_constant", cfg.get("line3_constant_mode_enabled", False)))
            if line3_rna_mode:
                sensorcorr_rna = cfg.get("sensorcorr_rna", None)
                if isinstance(sensorcorr_rna, str):
                    sensorcorr_rna = ast.literal_eval(sensorcorr_rna)
                if isinstance(sensorcorr_rna, (list, tuple)) and len(sensorcorr_rna) >= 5:
                    self.sensorcorr_extra_rna = [float(sensorcorr_rna[i]) for i in range(5)]
                    while len(self.sensorcorr) < 4:
                        self.sensorcorr.append([0, 0, 0, 0, 0])
                    if not bool(getattr(self, "extra_pump_connected", False)):
                        self.sensorcorr[3] = [float(sensorcorr_rna[i]) for i in range(5)]
        except Exception:
            pass
        
        # Parse FlushFRs
        flush_frs_val = cfg.get("FlushFRs", "[420,4,4,4]")
        if isinstance(flush_frs_val, str):
            try:
                flush_frs_val = ast.literal_eval(flush_frs_val)
            except:
                flush_frs_val = [420, 4, 4, 4]
        self.flush_frs = flush_frs_val

        # Parse StartFlush setpoints (fallback to FlushFRs for backward compatibility)
        start_flush1_val = cfg.get("StartFlush1FRs", flush_frs_val)
        if isinstance(start_flush1_val, str):
            try:
                start_flush1_val = ast.literal_eval(start_flush1_val)
            except Exception:
                start_flush1_val = flush_frs_val
        self.start_flush1_frs = start_flush1_val

        start_flush2_val = cfg.get("StartFlush2FRs", flush_frs_val)
        if isinstance(start_flush2_val, str):
            try:
                start_flush2_val = ast.literal_eval(start_flush2_val)
            except Exception:
                start_flush2_val = flush_frs_val
        self.start_flush2_frs = start_flush2_val
        
        # Flush options
        self.start_flush = bool(cfg.get("StartFlush", True))
        self.zero_flush = bool(cfg.get("ZeroFlush", False))
        # Backward compatibility: old single switch still supported as fallback.
        legacy_flush_ramp = bool(cfg.get("FlushRampEnabled", True))
        self.start_flush_ramp_enabled = bool(
            cfg.get("StartFlushRampEnabled", self.start_flush_ramp_enabled if "StartFlushRampEnabled" in cfg else legacy_flush_ramp)
        )
        self.zero_flush_ramp_enabled = bool(
            cfg.get("ZeroFlushRampEnabled", self.zero_flush_ramp_enabled if "ZeroFlushRampEnabled" in cfg else legacy_flush_ramp)
        )
        self.zero_flow_blocking = bool(cfg.get("ZeroFlowBlocking", True))
        self.equilibration_retry = bool(cfg.get("EquilibrationRetry", self.equilibration_retry))

        # Additional stabilization delays
        try:
            self.flush_time_s = float(cfg.get("flush_time_s", self.flush_time_s))
        except Exception:
            pass
        try:
            self.first_comp_delay_s = float(cfg.get("first_comp_delay_s", self.first_comp_delay_s))
        except Exception:
            pass
        try:
            self.min_nonzero_set_fr = float(cfg.get("min_nonzero_set_fr", self.min_nonzero_set_fr))
        except Exception:
            pass
        try:
            self.zero_block_hold_s = float(cfg.get("zero_block_hold_s", self.zero_block_hold_s))
        except Exception:
            pass
        try:
            self.prime_buffer_fr = float(cfg.get("prime_buffer_fr", self.prime_buffer_fr))
        except Exception:
            pass
        try:
            self.prime_lipid_fr = float(cfg.get("prime_lipid_fr", self.prime_lipid_fr))
        except Exception:
            pass
        try:
            self.prime_rna_buffer_fr = float(cfg.get("prime_rna_buffer_fr", self.prime_rna_buffer_fr))
        except Exception:
            pass
        try:
            self.rna_buffer_startflush_fr = float(cfg.get("rna_buffer_startflush_fr", self.rna_buffer_startflush_fr))
        except Exception:
            pass
        try:
            self.rna_buffer_zeroflush_fr = float(cfg.get("rna_buffer_zeroflush_fr", self.rna_buffer_zeroflush_fr))
        except Exception:
            pass
        try:
            self.prime_to_startflush_ramp_s = float(
                cfg.get("prime_to_startflush_ramp_s", self.prime_to_startflush_ramp_s)
            )
        except Exception:
            pass
        try:
            self.extra_flow_kp = float(cfg.get("extra_flow_kp", self.extra_flow_kp))
        except Exception:
            pass
        try:
            self.extra_flow_ki = float(cfg.get("extra_flow_ki", self.extra_flow_ki))
        except Exception:
            pass
        try:
            maxfrerror_val = cfg.get("maxfrerror", self.maxfrerror)
            if isinstance(maxfrerror_val, str):
                maxfrerror_val = ast.literal_eval(maxfrerror_val)
            if isinstance(maxfrerror_val, (list, tuple)) and len(maxfrerror_val) >= 2:
                self.maxfrerror = [float(maxfrerror_val[0]), float(maxfrerror_val[1])]
        except Exception:
            pass
